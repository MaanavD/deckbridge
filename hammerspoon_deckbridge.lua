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
    local url = clean(element:attributeValue("AXURL"))
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
