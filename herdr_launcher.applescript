-- Herdr is a terminal TUI, not a native GUI application. This applet gives it
-- a normal Applications/Spotlight launcher while keeping the official Homebrew
-- binary and persistent Herdr server as the implementation.
--
-- Important: do not activate Terminal before `do script`. If Terminal is not
-- already running, `activate` creates a blank login window and `do script`
-- creates a second Herdr window.

property herdrBinary : "/opt/homebrew/bin/herdr"
property herdrProfileName : "Herdr"
property herdrFontName : "JetBrainsMonoNFM-Regular"

on ensureHerdrProfile()
    tell application "Terminal"
        if exists settings set herdrProfileName then
            set herdrProfile to settings set herdrProfileName
        else
            set baseProfile to settings set "Clear Dark"
            set herdrProfile to make new settings set at end of settings sets with properties {name:herdrProfileName}
            set background color of herdrProfile to background color of baseProfile
            set normal text color of herdrProfile to normal text color of baseProfile
            set bold text color of herdrProfile to bold text color of baseProfile
            set cursor color of herdrProfile to cursor color of baseProfile
            set number of rows of herdrProfile to number of rows of baseProfile
            set number of columns of herdrProfile to number of columns of baseProfile
            set font size of herdrProfile to font size of baseProfile
        end if
        set font name of herdrProfile to herdrFontName
        return herdrProfile
    end tell
end ensureHerdrProfile

on focusExistingHerdrTab(herdrProfile)
    tell application "Terminal"
        repeat with terminalWindow in windows
            repeat with terminalTab in tabs of terminalWindow
                if "herdr" is in processes of terminalTab then
                    set current settings of terminalTab to herdrProfile
                    set selected of terminalTab to true
                    set frontmost of terminalWindow to true
                    activate
                    return true
                end if
            end repeat
        end repeat
    end tell
    return false
end focusExistingHerdrTab

on run
    set herdrProfile to ensureHerdrProfile()
    if focusExistingHerdrTab(herdrProfile) then return

    tell application "Terminal"
        set herdrTab to do script "exec " & quoted form of herdrBinary
        set current settings of herdrTab to herdrProfile
        set title displays custom title of herdrTab to true
        set custom title of herdrTab to "Herdr"
        activate
    end tell
end run
