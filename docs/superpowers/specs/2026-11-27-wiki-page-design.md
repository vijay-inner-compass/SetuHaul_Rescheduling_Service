# Wiki page: how to use the application

Status: **design approved** in conversation; implementation not started.

**Intent.** People using the screens get detailed instructions inside the application. The documents in `docs/` are written for
developers and demonstrations.

**What.** A `/wiki` page with a **Wiki** link in the top menu of the Warehouse, Admin and Gate screens. Every signed-in role
(admin, warehouse, security) sees all of it. One tab per area: Getting started, Warehouse, Admin, Gate, Driver chat. Each task
is a few numbered steps, including what common refusal messages mean.

**Not included.** Editing in the browser, search, other languages, the demo console, driver access to the page.

**Constraints.** Existing look and tabs, server-rendered, same security headers, no inline script or style, no private data
(no names, phones, keys or addresses). Instructions must describe what the screens actually do: use the screens' own labels
and messages.

**Approaches.** Text kept in the app (chosen: no storage or permissions, versioned with the code, so it can change in the same
commit as a screen). An editable wiki was set aside: it needs storage, roles and change history.
