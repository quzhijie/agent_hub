-- Focus an existing Agent Hub tab instead of opening a new one every click.
-- argv: 1 = dashboard URL to open if no tab exists, 2 = port (match token).
-- Matches any loopback tab on that port (127.0.0.1:PORT / *.localhost:PORT).
on run argv
	set target to item 1 of argv
	set portToken to item 2 of argv
	try
		if application "Google Chrome" is running then
			tell application "Google Chrome"
				set wi to 0
				repeat with w in windows
					set wi to wi + 1
					set ti to 0
					repeat with t in tabs of w
						set ti to ti + 1
						set u to URL of t
						if (u contains ("127.0.0.1:" & portToken)) or (u contains ("localhost:" & portToken)) then
							set active tab index of w to ti
							set index of w to 1
							activate
							return "focused"
						end if
					end repeat
				end repeat
				if (count of windows) is 0 then
					make new window
					set URL of active tab of front window to target
				else
					tell front window to make new tab with properties {URL:target}
				end if
				activate
				return "opened"
			end tell
		end if
	end try
	-- Chrome not running (or scripting failed): default browser.
	do shell script "open " & quoted form of target
	return "opened-default"
end run
