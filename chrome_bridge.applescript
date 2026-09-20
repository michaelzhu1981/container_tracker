-- Native Chrome automation for Chrome versions where JXA Application(...)
-- is rejected with -1743 but the Chrome AppleScript dictionary remains usable.

on replaceText(needle, replacement, sourceText)
    set savedDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to needle
    set pieces to text items of sourceText
    set AppleScript's text item delimiters to replacement
    set joined to pieces as text
    set AppleScript's text item delimiters to savedDelimiters
    return joined
end replaceText

on cleanField(value)
    set cleaned to value as text
    set cleaned to my replaceText(ASCII character 9, "\\t", cleaned)
    set cleaned to my replaceText(return, "\\r", cleaned)
    set cleaned to my replaceText(linefeed, "\\n", cleaned)
    return cleaned
end cleanField

on fieldSeparator()
    return ASCII character 9
end fieldSeparator

on joinLines(values)
    set savedDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to linefeed
    set joined to values as text
    set AppleScript's text item delimiters to savedDelimiters
    return joined
end joinLines

on run argv
    set actionName to item 1 of argv
    -- Item 2 is the expected normal-Chrome PID. AppleScript targets the
    -- registered non-Playwright Chrome instance; window/tab IDs keep all
    -- subsequent operations bound to the selected target.
    set windowId to item 3 of argv as integer
    set tabId to item 4 of argv as integer
    set payloadText to item 5 of argv
    set stageName to actionName
    try
        using terms from application "Google Chrome"
            tell application id "com.google.Chrome"
                if actionName is "inventory" then
                    set outputLines to {}
                    repeat with w in windows
                        set windowIdValue to id of w
                        repeat with t in tabs of w
                            set end of outputLines to (windowIdValue as text) & my fieldSeparator() & (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                        end repeat
                    end repeat
                    return "OK" & linefeed & my joinLines(outputLines)
                end if

                if actionName is "new_window" then
                    set stageName to "new_window"
                    set w to make new window
                    set t to active tab of w
                    set URL of t to payloadText
                    return "OK" & linefeed & (id of w as text) & my fieldSeparator() & (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                end if

                set stageName to "window"
                set w to first window whose id is windowId

                if actionName is "tabs" then
                    set outputLines to {}
                    repeat with t in tabs of w
                        set end of outputLines to (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                    end repeat
                    return "OK" & linefeed & my joinLines(outputLines)
                end if
                if actionName is "close_window" then
                    close w
                    return "OK" & linefeed & "true"
                end if
                if actionName is "new_tab" then
                    set stageName to "new_tab"
                    set t to make new tab at end of tabs of w with properties {URL:payloadText}
                    return "OK" & linefeed & (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                end if

                set stageName to "tab"
                set t to first tab of w whose id is tabId
                if actionName is "evaluate" then
                    set stageName to "evaluate"
                    set scriptResult to execute t javascript payloadText
                    return "OK" & linefeed & (scriptResult as text)
                end if
                if actionName is "tab" then
                    return "OK" & linefeed & (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                end if
                if actionName is "navigate" then
                    set URL of t to payloadText
                    return "OK" & linefeed & (id of t as text) & my fieldSeparator() & my cleanField(URL of t)
                end if
                if actionName is "close_tab" then
                    close t
                    return "OK" & linefeed & "true"
                end if
                if actionName is "activate" or actionName is "bounds" then
                    set tabIndex to 0
                    set candidateIndex to 0
                    repeat with candidate in tabs of w
                        set candidateIndex to candidateIndex + 1
                        if id of candidate is tabId then set tabIndex to candidateIndex
                    end repeat
                    if tabIndex is 0 then error "The tracking tab was closed or is no longer available." number -1728
                    set active tab index of w to tabIndex
                    set index of w to 1
                    activate
                    if actionName is "bounds" then
                        set box to bounds of w
                        return "OK" & linefeed & (item 1 of box as text) & "," & (item 2 of box as text) & "," & (item 3 of box as text) & "," & (item 4 of box as text)
                    end if
                    return "OK" & linefeed & "true"
                end if
                error "Unknown Chrome bridge action: " & actionName number -1708
            end tell
        end using terms from
    on error errorMessage number errorNumber
        set errorCode to "NAVIGATION"
        if errorNumber is -1743 or errorNumber is -10004 then
            set errorCode to "BROWSER_PERMISSION"
        else if stageName is "window" then
            set errorCode to "BROWSER_CLOSED"
        else if stageName is "tab" then
            set errorCode to "TAB_NOT_FOUND"
        end if
        return "ERROR" & my fieldSeparator() & errorCode & my fieldSeparator() & stageName & ": " & my cleanField(errorMessage) & " (" & errorNumber & ")"
    end try
end run
