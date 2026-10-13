## Deploy handoff -- batch YYYY-MM-DD (T-xxxx, T-yyyy)
Trunk:     <trunk> @ <sha> (pushed). Main worktree clean: `git status --porcelain` empty.
Command:   <the exact command the human runs, absolute paths, dry run first when the tool has one>
Summary:   <what changes, one line per item, each attributed to a ticket; if the human's own dry run differs in ANY way: stop, ping the owner>
Rollback:  <the exact undo>
Verify:    <per ticket: the exact command and the expected real output. A tool's "complete" line alone is not evidence.>
