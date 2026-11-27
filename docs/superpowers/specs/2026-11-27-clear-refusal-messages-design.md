# Clear refusal messages: design

**Intent.** When a request is refused, the message says which field or rule is wrong, so the user can fix it.

**Goals**
- `INVALID_DATES` has its own sentence for each case, with the dates: first date before today; last date before first date; more than 14 days.
- `INVALID_REQUEST` names each bad field and why, using the label shown on screen. The first bad field is marked where the form supports it.
- This covers every place the generic message appears, including the hand-written checks that raise `INVALID_REQUEST`.

**Non-goals.** No new validation rules, and no change to reason codes or status codes.

**Anti-goal.** A message never repeats a typed value (passwords, tokens). Dates in `INVALID_DATES` are the one exception.

**Approaches**
- Server describes the problem, page shows it (picked): one copy of the rules, covers every form.
- Browser checks before sending: quicker feedback, but duplicates the server rules and they drift.
- Both: most code, same drift.

**Left to the builder.** Response field names, exact wording.
