local DataStorage = require("datastorage")
local InfoMessage = require("ui/widget/infomessage")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local ltn12 = require("ltn12")
local lfs = require("libs/libkoreader-lfs")
local socket = require("socket")
local socketutil = require("socketutil")
local _ = require("gettext")

local PLUGIN_DIR = debug.getinfo(1, "S").source:match("^@(.-)[^/]*$") or "./"

local DashSync = WidgetContainer:extend{
    name = "dashsync",
    is_doc_only = false,
}

-- Only one sync at a time, even though the plugin is instantiated by both
-- the file manager and the reader.
local running = false

local function loadConfig()
    local ok, cfg = pcall(dofile, PLUGIN_DIR .. "config.lua")
    if not ok or type(cfg) ~= "table" then
        logger.warn("dashsync: cannot load config.lua", cfg)
        cfg = {}
    end
    cfg.max_age_hours = tonumber(cfg.max_age_hours) or 20
    cfg.retry_minutes = tonumber(cfg.retry_minutes) or 45
    cfg.dest = cfg.dest or "/mnt/us/dashboards"
    if cfg.wifi_off_after == nil then cfg.wifi_off_after = true end
    return cfg
end

local function stateFile()
    return LuaSettings:open(DataStorage:getSettingsDir() .. "/dashsync.lua")
end

local function configured(cfg)
    return type(cfg.base_url) == "string"
        and cfg.base_url:match("^https://")
        and not cfg.base_url:find("YOUR%-")
end

local function ensureDir(path)
    if lfs.attributes(path, "mode") == "directory" then return true end
    local acc = ""
    for part in path:gmatch("[^/]+") do
        acc = acc .. "/" .. part
        if lfs.attributes(acc, "mode") ~= "directory" then
            lfs.mkdir(acc)
        end
    end
    return lfs.attributes(path, "mode") == "directory"
end

local function httpGet(url, sink)
    local https = require("ssl.https")
    socketutil:set_timeout(15, 90)
    local code, headers, status = socket.skip(1, https.request{
        url = url,
        method = "GET",
        sink = socketutil.table_sink(sink),
        headers = { ["Cache-Control"] = "no-cache" },
    })
    socketutil:reset_timeout()
    return code, status
end

local function fetch(url)
    local chunks = {}
    local ok, code, status = pcall(httpGet, url, chunks)
    if not ok then
        socketutil:reset_timeout()
        return nil, tostring(code)
    end
    if code ~= 200 then
        return nil, "HTTP " .. tostring(code) .. " " .. tostring(status or "")
    end
    return table.concat(chunks)
end

local function writeAtomic(path, data)
    local tmp = path .. ".part"
    local f, err = io.open(tmp, "wb")
    if not f then return nil, err end
    f:write(data)
    f:close()
    os.remove(path)
    local ok, rerr = os.rename(tmp, path)
    if not ok then return nil, rerr end
    return true
end

-- Returns true, count on success; false, message on failure.
function DashSync:doSync(cfg)
    local json = require("json")
    local base = cfg.base_url:gsub("/+$", "")
    local body, err = fetch(base .. "/manifest.json")
    if not body then return false, "manifest: " .. tostring(err) end
    local ok, manifest = pcall(json.decode, body)
    if not ok or type(manifest) ~= "table" or type(manifest.pages) ~= "table" then
        return false, "manifest is not valid"
    end
    if not ensureDir(cfg.dest) then return false, "cannot create " .. cfg.dest end

    local sha2 = select(2, pcall(require, "ffi/sha2"))
    local files = {}
    for _i, name in ipairs(manifest.pages) do files[#files + 1] = name end
    local count = 0
    for _i, name in ipairs(files) do
        -- Names come from our own site, but never trust them for paths.
        if type(name) ~= "string" or name:find("/", 1, true) or name:find("..", 1, true) then
            return false, "bad file name in manifest"
        end
        local data, ferr = fetch(base .. "/" .. name)
        if not data or #data == 0 then return false, name .. ": " .. tostring(ferr or "empty") end
        local want = manifest.sha256 and manifest.sha256[name]
        if want and type(sha2) == "table" and sha2.sha256 then
            if sha2.sha256(data) ~= want then return false, name .. ": checksum mismatch" end
        end
        local wok, werr = writeAtomic(cfg.dest .. "/" .. name, data)
        if not wok then return false, name .. ": " .. tostring(werr) end
        count = count + 1
    end
    -- Remove pages that are no longer published (numbered pages only).
    local keep = {}
    for _i, n in ipairs(files) do keep[n] = true end
    for entry in lfs.dir(cfg.dest) do
        if (entry:match("^%d%d%-.+%.png$") or entry:match("^%d%d%-.+%.pdf$")) and not keep[entry] then
            os.remove(cfg.dest .. "/" .. entry)
        end
    end
    return true, count, manifest.generated_local
end

function DashSync:isStale(cfg, st)
    local now = os.time()
    local last_ok = tonumber(st:readSetting("last_ok")) or 0
    local last_try = tonumber(st:readSetting("last_try")) or 0
    if now - last_ok < cfg.max_age_hours * 3600 then return false end
    if now - last_try < cfg.retry_minutes * 60 then return false end
    return true
end

-- force = true when the user taps the menu item.
function DashSync:sync(force)
    if running then return end
    local cfg = loadConfig()
    if not configured(cfg) then
        if force then
            UIManager:show(InfoMessage:new{ text = _("Dashboard sync: set base_url in dashsync.koplugin/config.lua") })
        end
        return
    end
    local st = stateFile()
    if not force and not self:isStale(cfg, st) then return end

    running = true
    local was_online = NetworkMgr:isOnline()
    NetworkMgr:runWhenOnline(function()
        st:saveSetting("last_try", os.time())
        st:flush()
        local ok, a, b = pcall(self.doSync, self, cfg)
        if not ok then a, b = false, tostring(a) end
        if a then
            st:saveSetting("last_ok", os.time())
            st:saveSetting("last_msg", b or "")
            st:flush()
            logger.info("dashsync: synced", b)
        else
            logger.warn("dashsync: failed", b)
        end
        if cfg.wifi_off_after and not was_online then
            NetworkMgr:disableWifi()
        end
        running = false
        local fm = package.loaded["apps/filemanager/filemanager"]
        if a and fm and fm.instance and fm.instance.onRefresh then
            pcall(function() fm.instance:onRefresh() end)
        end
        if force or not a then
            UIManager:show(InfoMessage:new{
                text = a and _("Dashboard updated.") or (_("Dashboard sync failed: ") .. tostring(b)),
                timeout = 4,
            })
        end
    end)
    -- If the user cancels the Wi-Fi prompt the callback never runs.
    UIManager:scheduleIn(120, function() running = false end)
end

function DashSync:scheduleCheck(delay)
    UIManager:unschedule(self._check)
    UIManager:scheduleIn(delay, self._check, self)
end

function DashSync:_check()
    self:sync(false)
end

function DashSync:init()
    self.ui.menu:registerToMainMenu(self)
    -- On first load after launching KOReader.
    self:scheduleCheck(15)
end

-- Woke from sleep: give Wi-Fi/the OS a moment, then sync if stale.
function DashSync:onResume()
    self:scheduleCheck(8)
end

function DashSync:onSuspend()
    UIManager:unschedule(self._check)
end

function DashSync:addToMainMenu(menu_items)
    menu_items.dashsync = {
        text = _("Sync dashboard now"),
        sorting_hint = "tools",
        callback = function() self:sync(true) end,
    }
end

return DashSync
