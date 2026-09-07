"""End-to-end check for the editing, review and identity changes.

Not a test suite — there isn't one. This is the same shape as the scripts that
verified slices 1 and 2: drive the real API against the real database and the
real storage, and say plainly what happened.

Run with the server up:  python verify_slice3.py
"""

import sys
import uuid

import httpx

BASE = "http://localhost:8000"
EMAIL = "slice3-verify@example.com"
PASSWORD = "verify-slice3-pw"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok    {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30.0)

    # --- sign in -----------------------------------------------------------
    r = c.post("/dev/signin", json={"email": EMAIL, "password": PASSWORD})
    if r.status_code != 200:
        print(f"could not sign in: {r.status_code} {r.text[:300]}")
        return 1
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    me = c.get("/me", headers=auth).json()
    if not me["memoirs"]:
        # Fresh account: make a memoir the way onboarding does.
        d = c.post("/drafts", json={}).json()
        c.patch(
            f"/drafts/{d['id']}",
            headers={"X-Draft-Token": d["token"]},
            json={"subject_name": "Verify Subject", "relationship": "other"},
        )
        r = c.post(
            "/memoirs/claim",
            headers={**auth, "X-Draft-Token": d["token"]},
            json={"draft_id": d["id"]},
        )
        if r.status_code not in (200, 201):
            print(f"could not claim a memoir: {r.status_code} {r.text[:300]}")
            return 1
        me = c.get("/me", headers=auth).json()

    memoir = me["memoirs"][0]
    memoir_id = memoir["id"]
    link = memoir["link_token"]
    print(f"\nmemoir {memoir_id}\nlink   {link}\n")

    # --- A. editing text ---------------------------------------------------
    print("A. editing a memory")
    m = c.post(
        f"/memoirs/{memoir_id}/memories",
        headers=auth,
        json={"title": "First", "body_text": "The kitchen in August."},
    ).json()
    mid = m["id"]
    check("created as a text memory", m["kind"] == "text", m["kind"])
    check("carries participant_id", "participant_id" in m)
    check("is_owner is true for the owner", m.get("is_owner") is True)

    r = c.patch(
        f"/memories/{mid}",
        headers=auth,
        json={"title": "Renamed", "happened_on": "1988-08-12"},
    )
    check("title and date edited", r.status_code == 200, r.text[:200])
    check("title changed", r.json()["title"] == "Renamed")
    check("date changed", r.json()["happened_on"] == "1988-08-12")
    check("body left alone", r.json()["body_text"] == "The kitchen in August.")

    r = c.patch(f"/memories/{mid}", headers=auth, json={"body_text": ""})
    check("emptying a text-only memory is refused", r.status_code == 400, r.text[:200])
    still = c.get(f"/memories/{mid}", headers=auth).json()
    check("and the text survived the refusal", still["body_text"] == "The kitchen in August.")

    # --- A. editing media --------------------------------------------------
    print("\nA. adding and removing media")
    t = c.post(
        "/media/uploads",
        headers=auth,
        json={"memoir_id": memoir_id, "kind": "image", "mime_type": "image/jpeg"},
    ).json()
    httpx.put(t["upload_url"], content=b"\xff\xd8\xff\xdb not-a-real-jpeg",
              headers={"Content-Type": "image/jpeg"}, timeout=30.0)
    c.post(f"/media/uploads/{t['asset_id']}/complete", headers=auth)

    r = c.post(f"/memories/{mid}/assets", headers=auth,
               json={"asset_ids": [t["asset_id"]]})
    check("photo attached", r.status_code == 200, r.text[:200])
    check("kind re-derived to photo", r.json()["kind"] == "photo", r.json().get("kind"))
    check("asset is on the memory", len(r.json()["assets"]) == 1)

    r = c.delete(f"/memories/{mid}/assets/{t['asset_id']}", headers=auth)
    check("photo removed", r.status_code == 200, r.text[:200])
    check("kind re-derived back to text", r.json()["kind"] == "text", r.json().get("kind"))
    check("no assets left", len(r.json()["assets"]) == 0)

    # A photo-only memory: removing its only photo must be refused.
    t2 = c.post("/media/uploads", headers=auth,
                json={"memoir_id": memoir_id, "kind": "image",
                      "mime_type": "image/jpeg"}).json()
    httpx.put(t2["upload_url"], content=b"\xff\xd8\xff\xdb x",
              headers={"Content-Type": "image/jpeg"}, timeout=30.0)
    c.post(f"/media/uploads/{t2['asset_id']}/complete", headers=auth)
    only = c.post(f"/memoirs/{memoir_id}/memories", headers=auth,
                  json={"asset_ids": [t2["asset_id"]]}).json()
    r = c.delete(f"/memories/{only['id']}/assets/{t2['asset_id']}", headers=auth)
    check("removing the last thing in a memory is refused", r.status_code == 400,
          f"{r.status_code} {r.text[:150]}")
    back = c.get(f"/memories/{only['id']}", headers=auth).json()
    check("and the photo is still attached", len(back["assets"]) == 1)

    # A stranger's asset id cannot be removed through a memory you own.
    r = c.delete(f"/memories/{mid}/assets/{uuid.uuid4()}", headers=auth)
    check("unknown asset id is 404", r.status_code == 404, str(r.status_code))

    # --- E1. the name is honoured -----------------------------------------
    print("\nE1. a returning contributor's new name")
    r1 = c.post(f"/j/{link}/memories",
                json={"display_name": "Ali", "body_text": "I remember the garden."})
    check("first contribution accepted", r1.status_code == 201, r1.text[:200])
    receipt = r1.json()
    tok_a = receipt["participant_token"]
    check("attributed to Ali", receipt["memory"]["contributor_name"] == "Ali")
    check("is_owner is false for a contributor", receipt["memory"]["is_owner"] is False)

    r2 = c.post(f"/j/{link}/memories",
                json={"display_name": "Ali Raza", "body_text": "And the stairs.",
                      "participant_token": tok_a})
    check("second contribution accepted", r2.status_code == 201, r2.text[:200])
    check("new name honoured", r2.json()["memory"]["contributor_name"] == "Ali Raza",
          r2.json()["memory"]["contributor_name"])
    check("same person, same token", r2.json()["participant_token"] == tok_a)

    mine = c.get(f"/j/{link}/memories", headers={"X-Participant-Token": tok_a}).json()
    check("their earlier memory was renamed too",
          all(m["contributor_name"] == "Ali Raza" for m in mine),
          str([m["contributor_name"] for m in mine]))
    check("they see only their own", len(mine) == 2, str(len(mine)))

    # --- E2. second device, then merge -------------------------------------
    print("\nE2. the same person on a second device")
    r3 = c.post(f"/j/{link}/memories",
                json={"display_name": "Ali Raza", "body_text": "From my laptop."})
    tok_b = r3.json()["participant_token"]
    check("a second device is a second person", tok_b != tok_a)

    people = c.get(f"/memoirs/{memoir_id}/contributors", headers=auth).json()
    dupes = [p for p in people["participants"] if p["display_name"] == "Ali Raza"]
    check("both appear in the contributors list", len(dupes) == 2, str(len(dupes)))

    winner = max(dupes, key=lambda p: p["memory_count"])
    loser = min(dupes, key=lambda p: p["memory_count"])

    r = c.post(
        f"/memoirs/{memoir_id}/contributors/{loser['id']}/merge-into/{winner['id']}",
        headers=auth,
    )
    check("merge accepted", r.status_code == 200, r.text[:200])
    check("memories moved", r.json()["memories_moved"] == loser["memory_count"],
          str(r.json()))

    after = c.get(f"/memoirs/{memoir_id}/contributors", headers=auth).json()
    left = [p for p in after["participants"] if p["display_name"] == "Ali Raza"]
    check("now one entry, not two", len(left) == 1, str(len(left)))
    check("with all the memories", left[0]["memory_count"] == 3,
          str(left[0]["memory_count"]) if left else "-")

    # The key point of keeping the merged row: the loser's device still works.
    r4 = c.post(f"/j/{link}/memories",
                json={"display_name": "Ali Raza", "body_text": "Laptop again.",
                      "participant_token": tok_b})
    check("the merged device can still contribute", r4.status_code == 201, r4.text[:200])
    after2 = c.get(f"/memoirs/{memoir_id}/contributors", headers=auth).json()
    left2 = [p for p in after2["participants"] if p["display_name"] == "Ali Raza"]
    check("and does NOT create a third person", len(left2) == 1, str(len(left2)))
    check("its memory landed on the merged person", left2[0]["memory_count"] == 4,
          str(left2[0]["memory_count"]) if left2 else "-")

    # Refusals
    r = c.post(
        f"/memoirs/{memoir_id}/contributors/{winner['id']}/merge-into/{winner['id']}",
        headers=auth,
    )
    check("merging someone into themselves is refused", r.status_code == 400,
          str(r.status_code))
    owner_row = next(p for p in after2["participants"] if p["role"] == "owner")
    r = c.post(
        f"/memoirs/{memoir_id}/contributors/{owner_row['id']}/merge-into/{winner['id']}",
        headers=auth,
    )
    check("merging the owner is refused", r.status_code == 400, str(r.status_code))
    r = c.post(
        f"/memoirs/{memoir_id}/contributors/{uuid.uuid4()}/merge-into/{winner['id']}",
        headers=auth,
    )
    check("an unknown participant is 404", r.status_code == 404, str(r.status_code))

    # --- D. the owner can delete a contribution ----------------------------
    print("\nD. the owner reviews and deletes a contribution")
    theirs = [m for m in c.get(f"/memoirs/{memoir_id}/memories", headers=auth).json()
              if not m["is_owner"]]
    check("contributions are visible to the owner", len(theirs) >= 1, str(len(theirs)))
    check("each names its participant",
          all(m["participant_id"] for m in theirs))
    r = c.delete(f"/memories/{theirs[0]['id']}", headers=auth)
    check("owner can delete a contributed memory", r.status_code == 204,
          str(r.status_code))

    # --- ownership boundaries still hold -----------------------------------
    print("\nboundaries")
    r = c.post(f"/memories/{mid}/assets", json={"asset_ids": [str(uuid.uuid4())]})
    check("attaching without a token is 401/403", r.status_code in (401, 403),
          str(r.status_code))
    r = c.get(f"/memories/{uuid.uuid4()}", headers=auth)
    check("someone else's memory is 404, never 403", r.status_code == 404,
          str(r.status_code))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
