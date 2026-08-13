-- Trusted Accessibility bridge for native Claude desktop session state.
--
-- Hammerspoon already has a durable user grant on the target Mac. Keeping AX
-- inspection here avoids rebuilding Deckbridge Mic whenever Claude changes
-- its UI, which would cause macOS to revoke that helper's ad-hoc signature.

local json = hs.json

local function clean(value)
    return tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function lower(value)
    return string.lower(clean(value))
end

local function startsWith(value, prefix)
    return value:sub(1, #prefix) == prefix
end

local function scanElement(element, result, depth)
    if not element or depth > 45 or result.visited >= 8000 then return end
    result.visited = result.visited + 1

    local role = clean(element:attributeValue("AXRole"))
    local title = clean(element:attributeValue("AXTitle"))
    local description = clean(element:attributeValue("AXDescription"))
    local value = clean(element:attributeValue("AXValue"))
    local rawUrl = element:attributeValue("AXURL")
    -- Chromium may bridge NSURL as a Lua table rather than a string.
    local url = clean(type(rawUrl) == "table" and rawUrl.url or rawUrl)
    local text = lower(title .. " " .. description .. " " .. value)

    if url ~= "" and (url:find("claude.ai/chat/", 1, true)
            or url:find("claude.ai/epitaxy/local_", 1, true)) then
        result.url = result.url ~= "" and result.url or url
        if title ~= "" and title ~= "Claude" then result.title = title end
    end

    -- Claude exposes these as concise live-region strings. They are a stronger
    -- signal than spinners or elapsed time and remain stable for screen readers.
    if text:find("claude finished the response", 1, true) then
        result.done = true
    end
    if text:find("claude is responding", 1, true)
            or text:find("claude is thinking", 1, true)
            or text:find("stop response", 1, true)
            or text:find("stop generating", 1, true) then
        result.working = true
    end

    -- Permission/confirmation controls are actionable waits, but message body
    -- prose containing the same words is not. Restrict matching to buttons.
    if role == "AXButton" then
        local button = lower(title ~= "" and title or description)
        if button == "allow once" or button == "allow"
                or button == "approve" or button == "continue"
                or startsWith(button, "allow for this chat")
                or startsWith(button, "grant permission") then
            result.blocked = true
        end
    end

    local children = element:attributeValue("AXChildren")
    if children then
        for _, child in ipairs(children) do
            scanElement(child, result, depth + 1)
        end
    end
end

function deckbridgeClaudeSnapshot()
    local app = hs.application.get("Claude")
    if not app then return json.encode({sessions = {}}) end
    local axApp = hs.axuielement.applicationElement(app)
    local windows = axApp and axApp:attributeValue("AXWindows") or nil
    local focusedWindow = axApp and axApp:attributeValue("AXFocusedWindow") or nil
    local sessions = {}
    for _, window in ipairs(windows or {}) do
        local result = {
            title = clean(window:attributeValue("AXTitle")), url = "",
            visited = 0, blocked = false, working = false, done = false,
        }
        scanElement(window, result, 0)
        local status = result.blocked and "blocked"
            or result.working and "working"
            or result.done and "done"
            or "idle"
        table.insert(sessions, {
            title = result.title, url = result.url, status = status,
            focused = focusedWindow ~= nil and window == focusedWindow,
        })
    end
    return json.encode({sessions = sessions})
end

-- Select a T3 thread inside Hammerspoon's durable Accessibility identity.
-- macOS can deny a separately trusted helper when launchd is its responsible
-- parent, even though the same binary succeeds from Terminal. The hs CLI is
-- only IPC; all AX reads and presses therefore happen inside Hammerspoon.
local function scanT3(element, result, depth)
    if not element or depth > 45 or result.visited >= 8000 then return end
    result.visited = result.visited + 1
    local role = clean(element:attributeValue("AXRole"))
    local title = clean(element:attributeValue("AXTitle"))
    local description = clean(element:attributeValue("AXDescription"))
    local value = clean(element:attributeValue("AXValue"))
    local rawUrl = element:attributeValue("AXURL")
    local url = clean(type(rawUrl) == "table" and rawUrl.url or rawUrl)
    if result.url == "" and url:find("t3code://app/#/", 1, true) then
        result.url = url
    end
    if role == "AXButton" then
        for _, label in ipairs({title, description, value}) do
            local matches = result.target == "Back" and label == result.target
                or result.target ~= "" and label:find(result.target, 1, true)
            if matches then
                local position = element:attributeValue("AXPosition") or {}
                local size = element:attributeValue("AXSize") or {}
                local identity = role .. "|" .. label .. "|"
                    .. tostring(position.x) .. "," .. tostring(position.y) .. "|"
                    .. tostring(size.w) .. "," .. tostring(size.h)
                if not result.matchSeen[identity] then
                    result.matchSeen[identity] = true
                    table.insert(result.matches, element)
                end
                break
            end
        end
    end
    local children = element:attributeValue("AXChildren")
    if children then
        for _, child in ipairs(children) do scanT3(child, result, depth + 1) end
    end
end

local function t3Snapshot(app, target)
    local result = {target = target, matches = {}, matchSeen = {},
                    url = "", visited = 0}
    local axApp = hs.axuielement.applicationElement(app)
    local windows = axApp and axApp:attributeValue("AXWindows") or nil
    for _, window in ipairs(windows or {}) do scanT3(window, result, 0) end
    return result
end

local function clickT3Element(element)
    -- T3's Chromium controls advertise AXPress but currently ignore that
    -- action. A click at the exact Accessibility bounds works reliably and
    -- does not require guessing coordinates. Preserve the operator's pointer
    -- position because eventtap emits a real click at the target bounds.
    local position = element:attributeValue("AXPosition")
    local size = element:attributeValue("AXSize")
    if not position or not size then return false end
    local originalPosition = hs.mouse.absolutePosition()
    hs.eventtap.leftClick({x = position.x + size.w / 2,
                           y = position.y + size.h / 2})
    hs.mouse.absolutePosition(originalPosition)
    return true
end

function deckbridgeT3FocusB64(title64, session64)
    local title = hs.base64.decode(title64 or "") or ""
    local session = hs.base64.decode(session64 or "") or ""
    if title == "" or session == "" then return "" end
    local app = hs.application.get("com.t3tools.t3code")
        or hs.application.get("T3 Code (Alpha)")
    if not app then return "" end
    app:activate(true)
    hs.timer.usleep(50000)

    -- Settings hides the thread sidebar. Its Back button restores the previous
    -- thread; it is absent on the normal thread surface.
    local back = t3Snapshot(app, "Back")
    if #back.matches > 0 then
        clickT3Element(back.matches[1])
        hs.timer.usleep(100000)
    end

    for attempt = 1, 3 do
        local target = t3Snapshot(app, title)
        if #target.matches > 0 then
            local candidate = target.matches[((attempt - 1) % #target.matches) + 1]
            clickT3Element(candidate)
            for _ = 1, 20 do
                hs.timer.usleep(50000)
                local selected = t3Snapshot(app, "")
                local suffix = "/" .. session
                if selected.url:sub(-#suffix) == suffix
                        or selected.url:find(suffix .. "/", 1, true) then
                    return selected.url
                end
            end
        end
        hs.timer.usleep(100000)
    end
    return ""
end

local function decodeUrlBase64(value)
    local standard = (value or ""):gsub("-", "+"):gsub("_", "/")
    local remainder = #standard % 4
    if remainder > 0 then standard = standard .. string.rep("=", 4 - remainder) end
    return hs.base64.decode(standard) or ""
end

-- LaunchAgent children cannot reliably use hs's CLI/XPC connection on every
-- macOS release. LaunchServices can always deliver Hammerspoon's URL event to
-- the already-running, Accessibility-trusted GUI app. The tiny result file is
-- a request-scoped acknowledgement that lets the caller verify the exact route.
hs.urlevent.bind("deckbridge-t3-focus", function(_, params)
    local request = clean(params.request)
    local session = clean(params.session)
    if not request:match("^%d+%-%d+$")
            or not session:match("^[%x%-]+$") then return end
    local app = hs.application.get("com.t3tools.t3code")
        or hs.application.get("T3 Code (Alpha)")
    if not app then return end
    local function finish(route)
        local directory = os.getenv("HOME") .. "/.deckbridge/t3-focus-results"
        hs.fs.mkdir(os.getenv("HOME") .. "/.deckbridge")
        hs.fs.mkdir(directory)
        local temporary = directory .. "/." .. request .. ".tmp"
        local final = directory .. "/" .. request
        local handle = io.open(temporary, "w")
        if not handle then return end
        handle:write(route or "")
        handle:close()
        os.rename(temporary, final)
    end
    local title = decodeUrlBase64(params.title or "")
    local suffix = "/" .. session
    local attempts = 0
    local backCount = -1
    local function selectThread()
        attempts = attempts + 1
        local target = t3Snapshot(app, title)
        local candidate = #target.matches > 0
            and target.matches[((attempts - 1) % #target.matches) + 1] or nil
        if not candidate or not clickT3Element(candidate) then
            if attempts < 3 then hs.timer.doAfter(0.1, selectThread)
            else
                local front = hs.application.frontmostApplication()
                finish("error:no-unique-target:" .. tostring(#target.matches)
                    .. ":" .. title .. ":back=" .. tostring(backCount)
                    .. ":front=" .. (front and front:name() or ""))
            end
            return
        end
        local polls = 0
        local function verify()
            polls = polls + 1
            local selected = t3Snapshot(app, "").url
            if selected:sub(-#suffix) == suffix
                    or selected:find(suffix .. "/", 1, true) then
                finish(selected)
            elseif polls < 20 then
                hs.timer.doAfter(0.05, verify)
            elseif attempts < 3 then
                hs.timer.doAfter(0.1, selectThread)
            else
                finish("error:route:" .. selected)
            end
        end
        hs.timer.doAfter(0.05, verify)
    end
    app:activate(true)
    -- URL callbacks run on Hammerspoon's main loop. Every GUI transition uses
    -- a timer so activation and synthetic clicks can be delivered between AX
    -- scans instead of being blocked by a synchronous callback.
    hs.timer.doAfter(0.25, function()
        local back = t3Snapshot(app, "Back")
        backCount = #back.matches
        if #back.matches > 0 and clickT3Element(back.matches[1]) then
            hs.timer.doAfter(0.15, selectThread)
        else
            selectThread()
        end
    end)
end)
