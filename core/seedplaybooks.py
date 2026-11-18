"""THE THREE METHODS EVERY INSTALL STARTS WITH (founder 2026-09-28: "the
beauty of this product … why will we not give them this straight away?").

A project keeps three standing documents, each a playbook written by its own
method: the MAP (how it works — method `atlas`), the PLAN (what we intend —
method `plan`), and, for anything sold or published, the SHEET (what it
promises — method `prospectus`). The methods are ours; they reach every
tenant here, customer-neutral (no names of ours, no dates), and the hub's
★SEED rule `seed_standing_documents` tells every agent they exist.

Seeding is by the playbook store's own door (core/playbooks._ensure), once
per tenant per process, and it never overrides the customer:
  * a method missing and never seeded → written, and its text's hash kept;
  * a method whose text is still exactly what we seeded → updated to the
    current text when that changes;
  * a method the customer edited → theirs, left alone;
  * a method the customer deleted → stays deleted;
  * a playbook of that name that WE never seeded (the customer's own, or a
    tenant that wrote its own before this shipped) → theirs, never touched.
"""
from __future__ import annotations

import hashlib

ANNOTATION = "★SEED method — ships with every install; edit freely, it becomes yours"

DDL = """
CREATE TABLE IF NOT EXISTS seed_playbook (
    name  text PRIMARY KEY,
    sha   text NOT NULL,
    at    timestamptz NOT NULL DEFAULT now()
);
"""

ATLAS = """# atlas — how to map a project

An **atlas** is one project's map of itself: what exists, what talks to what, in which direction, what each part holds and roughly does, and the procedures that operate it. It is written for agents. A human version is rendered from it on request and never stored — the store holds the one copy.

Every project builds its atlas the same way. This page is the method. To use it, tell your agent: **"use playbook atlas to write the map of this project."**

## Names

- The root map: `<project>-map`. Example: `shop-map`, `api-map`.
- One plate per plane: `<project>-map-<plane>`. Example: `shop-map-payments`, `api-map-ops`.
- Procedures have no playbook of their own. They are sections inside the plate they belong to, and the root points at them by plate and section name: "to deploy: shop-map-ops, deploy".
- A project keeps 4 to 8 plates. Fewer and a plate becomes the blob nobody reads; more and two plates start describing one thing, which is where drift begins.

The planes are the project's own cut, not a fixed list. Choose them so that each is a page a session reads in one go and so that no wire needs two plates to explain. Typical cuts: for a service, client / runtime / service / data / ops / money; for a media pipeline, sources / models / pipeline / assets / publishing / ops; for a writing project, engine / agents / stories / evaluation / ops.

## The three rules

1. **Topology is drawn once, in the root.** A plate never restates the wires. Its first line names the root section it belongs to; it carries only its own detail and its procedures. Duplication is where drift starts, so there is none by construction.
2. **Values live nowhere in the atlas.** No caps, dials, prices, ports, counts, costs. A page says where a number lives — the env file, the table, the config, the registry — and never what it is. The one exception is a plate explicitly declared as the project's decided list (for example a price list), dated, and it says so in its header.
3. **Whoever changes a wire updates the map in the same session.** A new route, table, hook, store, worker or machine changes the root; a new or changed procedure changes its plate. Not later. The root carries a line `verified <date> against <commits or targets>`; a reader trusts the map exactly as far as that line.

## What the root map contains, in this order

1. Header: what the project is in two sentences (from its purpose), and the `verified` line.
2. The diagram: boxes and arrows, in a `mermaid` block, one arrow per wire with what travels on it. Direction matters more than anything else on the page.
3. The wires, listed: for each arrow, who initiates, what travels, what never travels. This is the section that catches wrong assumptions — "the server never calls the client" is a wire fact.
4. The stores: every place state lives, what it holds, what it never holds.
5. The index: each plate by name with one line, and each procedure by plate and section.

## What a plate contains

- First line: `Plate of <project>-map, section <name>.`
- For each part of its plane: what it is, what it holds, what it talks to and in which direction, what it never does, and roughly what happens when it runs — described, not measured.
- Where the values live: a list of pointers (file, table, endpoint), never the values.
- Procedures, each as its own section:
  - the name and when it is used
  - the exact commands in fenced blocks, one command per block, paths absolute
  - what to read back afterwards to know it worked — a write reporting success is not evidence
  - what has actually gone wrong doing it, only from observed failures, with the date; never guessed cautions

## How it is written

1. From the code and the live system, never from memory or old notes. Notes go stale; the code is what runs.
2. Draw the diagram first. If an arrow cannot be named with what travels on it, the wire is not understood yet — go and read it.
3. Verify every wire live before the root is saved: hit the health endpoints, list the routes, list the services, read a real log line for each flow. Write the `verified` line only after this.
4. Write the plates from the root's sections, one plane at a time. Each plate is verified the same way for its own procedures: run the read-back.
5. Save each page with `save_playbook`, root first, then plates. The root's index is the last thing written, because it lists what exists.

## What stays out

- Instructions to the reader other than the maintenance rule above. A map describes. "Never do X" belongs in a rule if it is law, or nowhere; a map that gives advice becomes a cage.
- Numbers, per rule 2.
- History. What something used to be is in the history; the map says what is.
- Copies. No MAP.md in a repository, no generated variant. One copy, in the store. A human reads a rendering made on request.

## Keeping it true

- A session that touches a wire finishes by updating the map. The Ledger catches "update the map" said and not done; walk it.
- When a plate's procedure is run and something differs from the page, the page is fixed in that session, and the `verified` line on the root is refreshed.
- A plate nobody has used in months is not wrong for being old; it is checked when next used.
- The project's rule (step 6 below) is what makes every session look: a map nobody is told about is a map nobody reads.

## Starting a project's atlas

```text
1. Read the project's purpose rule; the root's two sentences come from it.
2. List the machines, services, stores and clients that exist. Draw the arrows with what travels.
3. Verify each arrow live. Write <project>-map with the verified line.
4. Cut the planes (4-8). Write <project>-map-<plane> for each, procedures inside.
5. Write the root's index last. Say what was verified and against what.
6. Save a RULE for this project (save_rule, the project's own), so every session knows:
   "This project's atlas is the playbook <project>-map (root) with plates
   <project>-map-<plane>, ... Read the root before changing any wire; whoever
   changes a wire updates the map in the same session and refreshes its
   verified line."
```
"""

PLAN = """# plan — how a project's plan is kept

A project has three standing documents. The **map** (`<project>-map`, method `atlas`) says how it works. The **sheet** (`<product>-sheet`, method `prospectus`) says what it promises, for anything sold or published. The **plan** (`<project>-plan`, this method) says what we intend and why. Each holds one kind of thing and nothing of the other two.

To use it, tell your agent: **"use playbook plan to write the plan of this project."**

## What a plan is, and is not

A plan is INTENT: what was decided, what will be done next, what has been set aside, and what waits on the owner — the person who decides. It is the place ideas survive a session's end.

It is not:

- the **Ledger** — the Ledger fills itself from what is said and is delete-only; it catches, it does not intend. Walking the Ledger means each row dies or moves into the plan.
- a **note** — a note is where one session's thread stands and rolls off after five sessions.
- a **record** — a record is what became true; a plan item that came true leaves the plan and, if it is a decision, a measurement or a lesson, becomes a record.
- the **map** — the map describes the system as it is. When an item is built, the map gains the wire; the plan loses the item.
- a **procedure** — the how goes in a playbook or a map plate, never in the plan.

## Shape

One playbook per project, named `<project>-plan`. Four sections, in this order, and nothing else:

1. **Decided** — one line per decision in force: the date, who decided (the owner, or "proposal" when it is not yet theirs), and the why in a clause. A proposal is never written as decided.
2. **Next** — an ordered list, ten items at most. Each item is one line that names the thing and says what finished looks like. Order is priority; the top item is what the next session picks up.
3. **Parked** — ideas raised and not decided, each with the date raised and one line. No order.
4. **Open with the owner** — decisions only the owner can make (prices, publishing, money, anything irreversible), one line each, dated.

The first line under the title carries `walked <date> by <session or person>`: the last time someone read the whole plan and moved lines. A reader trusts the plan exactly that far.

## The laws

- **Intent only.** No status, no results, no how. A number or a wire in a plan is a sign it belongs elsewhere.
- **Finished means gone.** Whoever finishes an item removes it in the SAME session and puts what changed where it belongs: the wire in the map, the decision or measurement in a record, the code in a commit. The plan never carries a Done section: history is in version control, the records and the chat history.
- **A decision enters with its why and its decider, the day it is made.** A session that hears the owner decide writes the line before it does anything else.
- **The Ledger is walked into the plan.** At a work boundary, read the Ledger: a row that still matters moves into Next or Parked as one line, then is deleted from the Ledger; a row that does not, dies.
- **Next stays short.** More than ten means the rest is parked. Order is the only status a plan has.
- **One writer at a time.** Read the plan before rewriting it; a plan is rewritten whole, so a stale copy overwrites another session's lines.

## Procedures

### Start a project's plan

`get_playbook plan` (this), then `save_playbook <project>-plan` with the four sections and the `walked` line. Seed Decided from the decisions in force; seed Next from the notes and the Ledger, then delete the Ledger rows you moved.

### Walk the plan (at a work boundary, and before a compact)

1. `get_playbook <project>-plan`.
2. Remove what is finished; put its result where it belongs (map, record, commit).
3. `list_ledger`: move or delete every row.
4. Reorder Next; park what fell below ten.
5. Update the `walked` line; `save_playbook` the whole document.

### Read the plan (every session)

The first lines of Next open every session beside the notes. Next's top line is what the session picks up unless the owner says otherwise.
"""

PROSPECTUS = """# prospectus — how to write a product sheet

A **product sheet** is one product's statement of what it is and what it promises: why it is different, who it is for, what a customer gets, how it is used, its limits, its prices, what happens at the edges, and what we refuse to claim. It is the single source every web page, post, article, docs page, interface string and support answer about that product is written from. Nobody writes about a product from memory, from the code, or from an old page; they write from the sheet.

It is the public counterpart of the map. The **map** (`<project>-map`, method `atlas`) is the PRIVATE description of the mechanism and carries no promises. The **sheet** is what is told to the world: the promises, the numbers, the reasons to buy. The sheet must never carry the mechanism — a sheet a competitor could rebuild the system from is a leak, not a sheet. When the two seem to disagree, the map is what the system does and the sheet is what is said; the fix is to change one of them in the same session, never to write a page from the gap.

To use it, tell your agent: **"use playbook prospectus to write the sheet of this product."**

## Names

- The sheet: `<product>-sheet`. Example: `shop-sheet`, `app-sheet`.
- One sheet per product that is sold or published. Editions, tiers and plans live inside the one sheet; two sheets for one product is where two prices for one thing come from.
- The sheet has no plates. It is one page a writer reads in one go.

## The laws

1. **Facts, not prose.** Every line is a claim that could be checked. Marketing voice is the writer's job; a sheet that reads like ad copy will be copied instead of used. But the sheet decides WHICH facts matter and in what order — see law 7.
2. **Numbers live here.** Prices, plan limits, caps, retention windows, sizes, supported platforms — each with who decided it and when, or what measured it and when. This is the one page where values belong; the map points here and never restates them. A number the owner has not decided is written OPEN, never guessed.
3. **What we never claim is a section, and it is mandatory.** Every claim retired from a page or a string goes on the stop list with the date and reason, and stays. Stale claims do not share a vocabulary with the sentence that replaced them, so a writer cannot search for them; the list is the only defence. This is also where a claim that could be made but is refused is written down as refused.
4. **Whoever changes a customer-visible behaviour rewrites the sheet in the same session.** A feature, a limit, a plan, a supported platform, a data-handling change. The sheet carries `verified <date> against <what>` and `decided by <who> <date>`; a writer trusts it exactly that far.
5. **A supported platform is one that has been proven** on a real install. Partial platforms are absent, not qualified.
6. **Rules on where numbers may appear stay with the product.** A sheet says whether its prices may be quoted off its own pricing page (region-bound prices may not; global prices may). Every writer obeys the sheet's line.
7. **Lead with the differentiators; hide the mechanism.** The sheet opens with why the product is different — the few things a buyer cannot get elsewhere — and the whole sheet is ordered by what is advertised, not by how the thing is built. It carries what a customer needs and what is sold, and NOTHING of the internal wiring: not the vendors, not the protocol between the parts, not whatever is deliberately kept hidden. If a line would help a competitor rebuild the system, it belongs in the map, not the sheet.

## What a sheet contains, in this order

1. **Header.** What the product is, two sentences, in the customer's terms. The `verified` and `decided by` lines. Any OPEN figures named.
2. **Why it is different.** The handful of things no one else offers, stated plainly, each one a reason to buy. This section leads because it is what sells; everything after supports it.
3. **Who it is for.** The buyer and the user; the first and best-proven audience; what it is not.
4. **Editions.** The choice a buyer makes, and what is and is not changeable later.
5. **What the customer gets.** The capabilities, one claim each, in terms of what a customer DOES — never in terms of the component that implements it. Describe the outcome, not the machine.
6. **The core promise sections** the product turns on — expanded, because they are the sell.
7. **Using it.** Install requirements, supported platforms, what a first session is like. Only what a customer needs to get started.
8. **Plans and limits.** The table with the numbers (law 2), the unit, packs and add-ons.
9. **The edges.** What happens at the cap, offline, on deletion, on a forgotten secret, on the retention terms. Each a claim, each true today.
10. **Prices.** The list, dated, decided by whom, with the rule on where they may appear, and any launch or lock terms.
11. **What we never claim.** The stop list (law 3).
12. **Where to look.** The map (marked internal), the public places, the legal pages.

## How it is written

1. From the code, the live system, the map and the owner's decisions — never from an old page and never from memory.
2. Start with section 11. List what the current pages claim that is no longer true; that list is the reason the sheet is being written.
3. Write section 2 (why it is different) in the owner's own words for what the product wins on. This is the section that most often comes back wrong the first time — it is judgement about what matters, not a feature dump.
4. Write section 5 from the product's surface, but translate every internal name into a customer outcome (law 7).
5. Every number in the limits and prices sections is taken from where it lives, with its date and decider; OPEN where undecided.
6. Verify the promise, edge and data claims live or against the last green test run. Mark `unverified` anything that could not be verified; a writer may not use it.
7. Save with `save_playbook`. Say what was verified and against what.

## How it is read

- A web page is built from the sheet and nothing else; it never adds a claim the sheet does not carry, never a number marked OPEN, and never a mechanism detail the sheet withholds.
- Posts and articles take their claims and quoted numbers from the sheet, under its law-6 line.
- Docs and interface strings are checked against the capability and using-it sections.
- A support answer about what the product does or costs comes from the sheet; if the sheet is silent, the sheet is missing a line, added that session.

## What stays out

- How the machine works. That is the map, and it is private.
- Roadmap and "soon" — a sheet states what is sold today.
- Prose for humans — the pages render it; the sheet is read by the agents that write them.
- History other than the stop list.

## Keeping it true

- A session that moves a customer-visible behaviour finishes by rewriting the sheet; the Ledger catches "update the sheet" said and not done.
- When a writer finds the sheet silent on something it needs, the sheet is extended that session, not the page.
- Before any public change to a page, the writer reads the sheet's `verified` line; a sheet older than the last product change is rewritten first.

## Starting a product's sheet

```text
1. Read the product's purpose rule and its map root.
2. Read the live pages; write the stop list (section 11) first.
3. Write "why it is different" (section 2) in the owner's terms — the reasons to buy.
4. List the surface; write section 5 as customer outcomes, never internal names.
5. Take every number from where it lives, with date and decider; OPEN where undecided.
6. Hide the mechanism (law 7).
7. Save <product>-sheet with its verified and decided-by lines.
```
"""

METHODS: dict[str, str] = {"atlas": ATLAS, "plan": PLAN, "prospectus": PROSPECTUS}


def sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def ensure(cur) -> dict:
    """Bring the tenant's methods in line (see the module text). The caller
    holds the connection and commits. Returns {name: what happened}."""
    from . import store
    store.ensure_ddl(cur, "seed_playbook", DDL)
    out = {}
    for name, text in METHODS.items():
        text = text.strip()
        new = sha(text)
        cur.execute("SELECT sha FROM seed_playbook WHERE name = %s;", [name])
        rec = cur.fetchone()
        cur.execute("SELECT content FROM playbook WHERE name = %s;", [name])
        row = cur.fetchone()
        if row is None:
            if rec is None:
                # ON CONFLICT: two sessions starting at once both seed — the
                # second must be a no-op, never an error in its caller's read
                cur.execute("INSERT INTO playbook (name, content, annotation) VALUES (%s, %s, %s) "
                            "ON CONFLICT (name) DO NOTHING;", [name, text, ANNOTATION])
                cur.execute("INSERT INTO seed_playbook (name, sha) VALUES (%s, %s) "
                            "ON CONFLICT (name) DO NOTHING;", [name, new])
                out[name] = "seeded"
            else:
                out[name] = "deleted by the customer — left"
            continue
        if rec is None:
            out[name] = "the customer's own — left"
            continue
        have = sha(row[0])
        if have == new:
            out[name] = "current"
        elif have == rec[0]:
            cur.execute("UPDATE playbook SET content = %s, annotation = %s, updated_at = now() WHERE name = %s;",
                        [text, ANNOTATION, name])
            cur.execute("UPDATE seed_playbook SET sha = %s, at = now() WHERE name = %s;", [new, name])
            out[name] = "updated"
        else:
            out[name] = "edited by the customer — left"
    return out
