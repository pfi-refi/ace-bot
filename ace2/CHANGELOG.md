# What changed in Ace

Ace reads the top of this file every turn (it rides in the cached half of his context, so it
costs almost nothing). **Newest first.** Keep each entry to what it means for Brady — not the
commit message, and not the implementation.

Why this exists: on 6 Sept, twice in one morning, Ace was found not to know things about
himself. His memory still said the upgrade session was a future plan, and his own board
context had no idea the records/actions model existed. Both were fixed by hand. A changelog
he reads is the fix that holds.

---

## 2026-09-06 — He can see the board's structure

Rows in his context now carry `[RECORD]`, `[PARKED · who]`, `[SETTLED]` and their lane. He had
the model in the database since yesterday but rendered a flat list, so he treated something
parked on someone else exactly like a to-do Brady owed. Six rows were parked and he could not
tell.

## 2026-09-06 — The bill register matches the budget sheet

Eight rows were wrong. Capital One is **$106**, not $80 — the minimum was raised. Ollo is
**$27**, not $50. Sewer is the full **$114.14** (arrears plus usage), Klarna **$123.71**,
mortgage **$1,167.92**. Patel and Venture 1 were missing from the register entirely and are now
on it. Money comes from the sheet; board prose is never the source for an amount.

## 2026-09-06 — Chat is readable again

History no longer renders dimmed when Brady reopens the app. Tool activity collapses to a
single line that ends up *above* the reply, so the last thing on screen is always what Ace
said rather than a stack of receipts.

## 2026-09-06 — Cost

The memory block (~4,600 tokens) moved into the cached part of the prompt, and the 1-hour cache
is on — it needed a beta header that was missing when it broke everything on 3 Aug. Every model
call now logs what it cached, so this is measurable instead of assumed.

## 2026-09-05 — He can read his own source

`read_own_code` searches and reads the app's own files. When Brady says something Ace produced
was wrong and Ace cannot explain why, he looks — and cites `file:line`. **Read only.** A fix he
identifies becomes a board item a person applies; he does not change or deploy himself.

## 2026-09-05 — Records and actions are different things

An ACTION ends when it is done. A RECORD has a state and is updated forever — a client, a bill,
a goal. A record can be `waiting` (parked on someone else, and protected from auto-closing) or
`settled` (finished but kept). This is why completing "drop off her packet" used to delete Feliz
the client. Five lanes file actions by whose time they take: GFI/PFI, Groundworks, Side Work,
Personal, Ace.

## 2026-09-05 — The brief stopped being blind

It received real items for Money and Bills only; every other category arrived as a bare count,
so a session due that day was invisible to it. It now gets the items, and follows three rules:
the conversation outranks the board, never assert progress it has not verified, and what Brady
says in front of a third party is positioning rather than a plan.

## 2026-09-05 — Voice stops answering half a sentence

A 1.2-second breath used to end Brady's turn, so "No." shipped on its own and Ace answered it.
Finished sentences still send immediately; unfinished ones wait for the rest.

## 2026-09-05 — Dates stop sliding

"Tomorrow" is frozen to a real date when it is written, so an item cannot claim to be due
tomorrow for four days running.
