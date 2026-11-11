#!/usr/bin/env python3
"""
ledger.py - the corpus ledger: the one component that decides which text may be evidence.

Run:  python3 ledger.py          (runs the self-tests, exits non-zero on any failure)

Lodestar's indexes return chunk IDs and scores, never evidence. A chunk becomes evidence only
when it is hydrated through the ledger, which checks three things at that moment:

    the chunk belongs to the LIVE version of its document,
    the document is ACTIVE (not deleted, not blacked out),
    the asking user is PERMITTED to read it.

Because the check happens at read time against the authoritative record, an update, a delete
or a permission revocation takes effect when its transaction commits - not when the vector
index, the lexical index, the answer cache and the conversation store catch up. The 5-minute
SLA (doc 09) becomes a deadline for cleaning up copies, not a deadline for correctness.

This file simulates the ledger, two lagging indexes, the purge worker, the purge auditor, a
snapshot restore, the answer cache and the conversation store, with a fake clock in seconds.
It encodes these rules:

  1. Monotonic flip: an older version that finishes ingesting late can never become live.
  2. Swap or blackout: an update swaps atomically if the new version is ready in time;
     otherwise the old version is hidden at upload + 270 s, and answers say so.
  3. Hydration gate: deleted, superseded, blacked-out or unpermitted chunks never pass,
     whatever the indexes return.
  4. Delivery check: an answer whose evidence was deleted while it was being generated is
     withheld at delivery time.
  5. Every deletion is a ledger entry, purged per store, and audited; a store that has not
     confirmed by 300 s is an SLA breach that pages someone.
  6. A restore is not finished until the deletion ledger has been replayed past the snapshot.
  7. The answer cache validates on read: an entry citing a version that is no longer live,
     or a document the reader may not see, is a miss.
  8. Permission staleness is asymmetric: a stale grant is a recall miss, a stale revocation
     can never be a leak.

Stdlib only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

FRESHNESS_SLA_S = 300
BLACKOUT_AFTER_S = 270          # hide the old version 30 s before the SLA, leaving margin

ACTIVE, DELETED, BLACKOUT = "ACTIVE", "DELETED", "BLACKOUT"
INGESTING, LIVE, SUPERSEDED, FAILED = "INGESTING", "LIVE", "SUPERSEDED", "FAILED"
ORG_WIDE, RESTRICTED = "org_wide", "restricted"


@dataclass
class Version:
    version_id: str
    doc_id: str
    seq: int
    status: str
    uploaded_at: float
    live_at: Optional[float] = None


@dataclass
class Doc:
    doc_id: str
    visibility: str
    acl: set
    status: str = ACTIVE
    live_version: Optional[str] = None
    requested_seq: int = 0
    pending_since: Optional[float] = None   # upload time of the newest not-yet-live version


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    version_id: str
    text: str


@dataclass
class DeletionEntry:
    seq: int
    doc_id: str
    version_ids: list
    committed_at: float
    purged_at: dict = field(default_factory=dict)    # store name -> time confirmed


class Index:
    """A derived projection. It lags, it can be restored from an old snapshot, and its copy of
    the ACL can be stale. None of that may change what an answer is allowed to cite."""

    def __init__(self, name: str):
        self.name = name
        self.rows: dict = {}            # chunk_id -> (doc_id, version_id, acl_copy, visibility)

    def upsert(self, chunk: Chunk, doc: Doc) -> None:
        self.rows[chunk.chunk_id] = (chunk.doc_id, chunk.version_id, set(doc.acl), doc.visibility)

    def search(self, principals: set) -> list:
        """Pre-filters on its own (possibly stale) ACL copy. Returns candidate chunk IDs."""
        return [cid for cid, (_, _, acl, vis) in self.rows.items()
                if vis == ORG_WIDE or acl & principals]

    def delete_where(self, doc_id: str, version_ids: Optional[list] = None) -> None:
        self.rows = {cid: r for cid, r in self.rows.items()
                     if not (r[0] == doc_id and (version_ids is None or r[1] in version_ids))}

    def count_doc(self, doc_id: str) -> int:
        return sum(1 for r in self.rows.values() if r[0] == doc_id)

    def snapshot(self) -> dict:
        return {k: (d, v, set(a), vis) for k, (d, v, a, vis) in self.rows.items()}

    def restore(self, snap: dict) -> None:
        self.rows = {k: (d, v, set(a), vis) for k, (d, v, a, vis) in snap.items()}


class Ledger:
    def __init__(self):
        self.docs: dict = {}
        self.versions: dict = {}
        self.chunks: dict = {}            # the authoritative chunk store: chunk_id -> Chunk
        self.deletions: list = []
        self.purge_queue: list = []       # outbox: (doc_id, version_ids, reason)
        self._del_seq = 0

    # ---- writes ---------------------------------------------------------------------
    def create(self, doc_id: str, visibility: str = ORG_WIDE, acl: Optional[set] = None) -> None:
        self.docs[doc_id] = Doc(doc_id, visibility, set(acl or ()))

    def register_upload(self, doc_id: str, t: float) -> Version:
        doc = self.docs[doc_id]
        doc.requested_seq += 1
        v = Version(f"{doc_id}@v{doc.requested_seq}", doc_id, doc.requested_seq, INGESTING, t)
        self.versions[v.version_id] = v
        doc.pending_since = t
        return v

    def ingest_ready(self, version_id: str, texts: list, indexes: list, t: float) -> bool:
        """Chunks are written to the chunk store and every index BEFORE the flip. The flip is a
        single conditional update: it succeeds only if this is still the newest request."""
        v = self.versions[version_id]
        doc = self.docs[v.doc_id]
        if doc.status == DELETED:
            v.status = SUPERSEDED
            return False
        new_chunks = [Chunk(f"{version_id}#{i}", v.doc_id, version_id, txt)
                      for i, txt in enumerate(texts)]
        for c in new_chunks:
            self.chunks[c.chunk_id] = c
            for ix in indexes:
                ix.upsert(c, doc)
        # UPDATE documents SET live_version=$v WHERE doc_id=$d AND requested_seq=$seq
        if v.seq != doc.requested_seq:
            v.status = SUPERSEDED
            self._purge_versions(doc.doc_id, [version_id], "late older version")
            return False
        old = doc.live_version
        doc.live_version, v.status, v.live_at = version_id, LIVE, t
        doc.pending_since = None
        if doc.status == BLACKOUT:
            doc.status = ACTIVE
        if old:
            self.versions[old].status = SUPERSEDED
            self._purge_versions(doc.doc_id, [old], "superseded")
        return True

    def tick(self, t: float) -> list:
        """Enforce the blackout half of swap-or-blackout. Returns the docs blacked out now."""
        hidden = []
        for doc in self.docs.values():
            if (doc.status == ACTIVE and doc.live_version and doc.pending_since is not None
                    and t - doc.pending_since >= BLACKOUT_AFTER_S):
                doc.status = BLACKOUT
                hidden.append(doc.doc_id)
        return hidden

    def delete(self, doc_id: str, t: float) -> DeletionEntry:
        """One transaction: mark deleted, remove chunk text, append to the deletion ledger,
        and write the outbox event that drives the purge workers."""
        doc = self.docs[doc_id]
        doc.status = DELETED
        vids = [v.version_id for v in self.versions.values() if v.doc_id == doc_id]
        self.chunks = {k: c for k, c in self.chunks.items() if c.doc_id != doc_id}
        self._del_seq += 1
        entry = DeletionEntry(self._del_seq, doc_id, vids, t)
        self.deletions.append(entry)
        self.purge_queue.append((doc_id, None, "deleted"))
        return entry

    def set_acl(self, doc_id: str, acl: set) -> None:
        self.docs[doc_id].acl = set(acl)     # indexes learn about this later, if ever

    def _purge_versions(self, doc_id: str, vids: list, reason: str) -> None:
        self.chunks = {k: c for k, c in self.chunks.items() if c.version_id not in vids}
        self.purge_queue.append((doc_id, vids, reason))

    # ---- reads ----------------------------------------------------------------------
    def permitted(self, doc: Doc, principals: set) -> bool:
        return doc.visibility == ORG_WIDE or bool(doc.acl & principals)

    def hydrate(self, chunk_ids: list, principals: set) -> list:
        """The gate. SELECT ... FROM chunks JOIN documents ... WHERE status='ACTIVE' AND
        chunks.version_id = documents.live_version AND <acl check>."""
        out = []
        for cid in chunk_ids:
            c = self.chunks.get(cid)
            if c is None:
                continue
            doc = self.docs[c.doc_id]
            if doc.status != ACTIVE or c.version_id != doc.live_version:
                continue
            if not self.permitted(doc, principals):
                continue
            out.append(c)
        return out

    def deliverable(self, cited_versions: set, principals: set) -> bool:
        """Delivery-time check: every (doc_id, version_id) the answer cites is still live and
        still readable by this user."""
        for doc_id, vid in cited_versions:
            doc = self.docs.get(doc_id)
            if (doc is None or doc.status != ACTIVE or doc.live_version != vid
                    or not self.permitted(doc, principals)):
                return False
        return True

    def doc_notice(self, doc_id: str) -> Optional[str]:
        doc = self.docs.get(doc_id)
        if doc is None or doc.status == DELETED:
            return "removed"
        if doc.status == BLACKOUT:
            return "being updated"
        return None


class PurgeWorker:
    def __init__(self, ledger: Ledger, stores: dict):
        self.ledger, self.stores = ledger, stores
        self.broken: set = set()          # store names whose delete call silently fails

    def run(self, t: float) -> None:
        queue, self.ledger.purge_queue = self.ledger.purge_queue, []
        for doc_id, vids, reason in queue:
            for name, store in self.stores.items():
                if name in self.broken:
                    continue
                store.delete_where(doc_id, vids)
                if reason == "deleted":
                    for e in self.ledger.deletions:
                        if e.doc_id == doc_id:
                            e.purged_at.setdefault(name, t)


class Auditor:
    """Does not trust the purge worker's own report: it asks each store directly."""

    def __init__(self, ledger: Ledger, stores: dict):
        self.ledger, self.stores = ledger, stores

    def run(self, t: float) -> list:
        breaches = []
        for e in self.ledger.deletions:
            for name, store in self.stores.items():
                still_there = hasattr(store, "count_doc") and store.count_doc(e.doc_id) > 0
                if still_there and t - e.committed_at > FRESHNESS_SLA_S:
                    breaches.append((e.doc_id, name, round(t - e.committed_at)))
        return breaches


def restore_with_replay(index: Index, snap: dict, snapshot_time: float, ledger: Ledger) -> int:
    """Restore a snapshot, then re-apply every deletion committed after it was taken, and
    every version superseded since. Returns the number of documents re-purged."""
    index.restore(snap)
    replayed = 0
    for e in ledger.deletions:
        if e.committed_at > snapshot_time and index.count_doc(e.doc_id):
            index.delete_where(e.doc_id)
            replayed += 1
    live = {d.live_version for d in ledger.docs.values() if d.live_version}
    stale = [cid for cid, (doc_id, vid, _, _) in index.rows.items() if vid not in live]
    for cid in stale:
        del index.rows[cid]
    return replayed


class AnswerCache:
    """Exact cache, keyed on everything that can change the answer, validated on read."""

    def __init__(self):
        self.entries: dict = {}

    @staticmethod
    def key(canonical_q: str, filters: tuple, permission_fp: str, lang: str) -> tuple:
        return (canonical_q, filters, permission_fp, lang)

    def put(self, key: tuple, answer: str, cited_versions: set) -> None:
        self.entries[key] = (answer, set(cited_versions))

    def get(self, key: tuple, ledger: Ledger, principals: set) -> Optional[str]:
        hit = self.entries.get(key)
        if hit is None:
            return None
        answer, cited = hit
        if not ledger.deliverable(cited, principals):
            del self.entries[key]          # a stale entry is deleted, never served
            return None
        return answer

    def delete_where(self, doc_id: str, version_ids: Optional[list] = None) -> None:
        self.entries = {k: (a, c) for k, (a, c) in self.entries.items()
                        if not any(d == doc_id and (version_ids is None or v in version_ids)
                                   for d, v in c)}

    def count_doc(self, doc_id: str) -> int:
        return sum(1 for _, c in self.entries.values() if any(d == doc_id for d, _ in c))


class ConversationStore:
    """Turns store references to evidence, not copies of it. The claim text the model wrote is
    the one derived copy, so it is indexed by cited doc_id and redacted when the doc goes."""

    def __init__(self):
        self.turns: dict = {}              # turn_id -> {"claims": [(text, chunk_id, doc_id)]}

    def add(self, turn_id: str, claims: list) -> None:
        self.turns[turn_id] = {"claims": list(claims)}

    def render(self, turn_id: str, ledger: Ledger, principals: set) -> list:
        out = []
        for text, chunk_id, doc_id in self.turns[turn_id]["claims"]:
            got = ledger.hydrate([chunk_id], principals)
            if got:
                out.append((text, got[0].text))
            else:
                note = ledger.doc_notice(doc_id) or "superseded"
                out.append((text, f"[source {note}]"))
        return out

    def delete_where(self, doc_id: str, version_ids: Optional[list] = None) -> None:
        if version_ids is not None:
            return                          # an update keeps history, rendering shows v-next
        for turn in self.turns.values():
            turn["claims"] = [("[removed: cited a deleted document]", c, d) if d == doc_id
                              else (t, c, d) for t, c, d in turn["claims"]]

    def count_doc(self, doc_id: str) -> int:
        return sum(1 for turn in self.turns.values() for t, _, d in turn["claims"]
                   if d == doc_id and not t.startswith("[removed"))


# ---------------------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------------------

def _world():
    L = Ledger()
    vec, lex = Index("vector"), Index("lexical")
    cache, convo = AnswerCache(), ConversationStore()
    stores = {"vector": vec, "lexical": lex, "answer_cache": cache, "conversations": convo}
    return L, vec, lex, cache, convo, stores


def _tests() -> list:
    t = []

    def case(name):
        def deco(fn):
            t.append((name, fn))
            return fn
        return deco

    @case("a late-finishing older version can never become live")
    def _():
        L, vec, lex, *_ = _world()
        L.create("FIN-POL-017")
        v1 = L.register_upload("FIN-POL-017", 0)
        L.ingest_ready(v1.version_id, ["per-diem EUR 80"], [vec, lex], 30)
        v2 = L.register_upload("FIN-POL-017", 100)
        v3 = L.register_upload("FIN-POL-017", 110)
        assert L.ingest_ready(v3.version_id, ["per-diem EUR 85"], [vec, lex], 140)
        assert not L.ingest_ready(v2.version_id, ["per-diem EUR 82"], [vec, lex], 150)
        assert L.docs["FIN-POL-017"].live_version == v3.version_id
        texts = [c.text for c in L.hydrate(vec.search(set()), set())]
        assert texts == ["per-diem EUR 85"], texts

    @case("an update ready inside the SLA swaps atomically and never blacks out")
    def _():
        L, vec, lex, *_ = _world()
        L.create("D")
        L.ingest_ready(L.register_upload("D", 0).version_id, ["old"], [vec, lex], 10)
        v2 = L.register_upload("D", 100)
        assert [c.text for c in L.hydrate(vec.search(set()), set())] == ["old"]
        assert L.tick(160) == []
        L.ingest_ready(v2.version_id, ["new"], [vec, lex], 160)
        assert [c.text for c in L.hydrate(vec.search(set()), set())] == ["new"]

    @case("an update not ready by upload+270s hides the old version, and says so")
    def _():
        L, vec, lex, *_ = _world()
        L.create("D")
        L.ingest_ready(L.register_upload("D", 0).version_id, ["old"], [vec, lex], 10)
        v2 = L.register_upload("D", 100)
        assert L.tick(369) == []
        assert L.tick(370) == ["D"]
        assert L.hydrate(vec.search(set()), set()) == []
        assert L.doc_notice("D") == "being updated"
        L.ingest_ready(v2.version_id, ["new"], [vec, lex], 700)
        assert [c.text for c in L.hydrate(vec.search(set()), set())] == ["new"]
        assert L.doc_notice("D") is None

    @case("a deletion is effective at commit, before any index has been purged")
    def _():
        L, vec, lex, cache, convo, stores = _world()
        L.create("D")
        L.ingest_ready(L.register_upload("D", 0).version_id, ["secret"], [vec, lex], 10)
        L.delete("D", 1000)
        assert vec.count_doc("D") == 1 and lex.count_doc("D") == 1   # indexes still have it
        assert L.hydrate(vec.search(set()) + lex.search(set()), set()) == []
        PurgeWorker(L, stores).run(1012)
        assert vec.count_doc("D") == 0 and lex.count_doc("D") == 0
        assert Auditor(L, stores).run(1060) == []

    @case("the auditor pages when one store silently failed to purge past 300s")
    def _():
        L, vec, lex, cache, convo, stores = _world()
        L.create("D")
        L.ingest_ready(L.register_upload("D", 0).version_id, ["x"], [vec, lex], 10)
        L.delete("D", 1000)
        w = PurgeWorker(L, stores)
        w.broken = {"lexical"}
        w.run(1012)
        assert Auditor(L, stores).run(1200) == []                  # inside the SLA, retrying
        assert Auditor(L, stores).run(1301) == [("D", "lexical", 301)]
        assert L.hydrate(lex.search(set()), set()) == []           # and still never cited

    @case("an answer whose evidence is deleted mid-generation is withheld at delivery")
    def _():
        L, vec, lex, *_ = _world()
        L.create("D")
        v = L.register_upload("D", 0)
        L.ingest_ready(v.version_id, ["fact"], [vec, lex], 10)
        evidence = L.hydrate(vec.search(set()), set())
        cited = {(c.doc_id, c.version_id) for c in evidence}
        assert L.deliverable(cited, set())
        L.delete("D", 2001)                                        # during generation
        assert not L.deliverable(cited, set())

    @case("restoring a snapshot resurrects deleted docs until the ledger is replayed")
    def _():
        L, vec, lex, cache, convo, stores = _world()
        for d in ("A", "B"):
            L.create(d)
            L.ingest_ready(L.register_upload(d, 0).version_id, [d], [vec, lex], 10)
        snap = vec.snapshot()                                      # taken at t=3000
        L.delete("B", 3100)
        PurgeWorker(L, stores).run(3105)
        vec.restore(snap)                                          # the naive restore
        assert vec.count_doc("B") == 1
        assert L.hydrate(vec.search(set()), set())[0].doc_id == "A"  # gate still holds
        replayed = restore_with_replay(vec, snap, 3000, L)
        assert replayed == 1 and vec.count_doc("B") == 0
        assert Auditor(L, stores).run(3500) == []

    @case("restore also drops versions superseded after the snapshot")
    def _():
        L, vec, lex, *_ = _world()
        L.create("D")
        L.ingest_ready(L.register_upload("D", 0).version_id, ["v1"], [vec, lex], 10)
        snap = vec.snapshot()
        L.ingest_ready(L.register_upload("D", 50).version_id, ["v2"], [vec, lex], 60)
        restore_with_replay(vec, snap, 20, L)
        assert all(vid == L.docs["D"].live_version for _, vid, _, _ in vec.rows.values())

    @case("a revoked permission is enforced at once, even though the index copy is stale")
    def _():
        L, vec, lex, *_ = _world()
        L.create("LEGAL-118", RESTRICTED, {"grp:legal"})
        L.ingest_ready(L.register_upload("LEGAL-118", 0).version_id, ["clause"], [vec, lex], 10)
        assert L.hydrate(vec.search({"grp:legal"}), {"grp:legal"})
        L.set_acl("LEGAL-118", {"grp:legal-partners"})
        assert vec.search({"grp:legal"})                            # stale index still matches
        assert L.hydrate(vec.search({"grp:legal"}), {"grp:legal"}) == []

    @case("a new grant the index has not seen yet is a recall miss, not a leak")
    def _():
        L, vec, lex, *_ = _world()
        L.create("LEGAL-118", RESTRICTED, {"grp:legal"})
        L.ingest_ready(L.register_upload("LEGAL-118", 0).version_id, ["clause"], [vec, lex], 10)
        L.set_acl("LEGAL-118", {"grp:legal", "grp:finance"})
        assert vec.search({"grp:finance"}) == []                   # missed until reindexed
        vec.upsert(L.chunks[next(iter(L.chunks))], L.docs["LEGAL-118"])
        assert L.hydrate(vec.search({"grp:finance"}), {"grp:finance"})

    @case("the answer cache misses when a cited version is superseded or unreadable")
    def _():
        L, vec, lex, cache, *_ = _world()
        L.create("D", RESTRICTED, {"grp:hr"})
        v1 = L.register_upload("D", 0)
        L.ingest_ready(v1.version_id, ["24 days"], [vec, lex], 10)
        k = AnswerCache.key("annual leave days", (), "fp:hr", "en")
        cache.put(k, "24 days [D p.4]", {("D", v1.version_id)})
        assert cache.get(k, L, {"grp:hr"}) == "24 days [D p.4]"
        assert cache.get(k, L, {"grp:sales"}) is None              # same key, wrong reader
        cache.put(k, "24 days [D p.4]", {("D", v1.version_id)})
        L.ingest_ready(L.register_upload("D", 100).version_id, ["26 days"], [vec, lex], 120)
        assert cache.get(k, L, {"grp:hr"}) is None and k not in cache.entries

    @case("conversation history shows '[source removed]' and the purge redacts claim text")
    def _():
        L, vec, lex, cache, convo, stores = _world()
        L.create("D")
        v = L.register_upload("D", 0)
        L.ingest_ready(v.version_id, ["EUR 85 per day"], [vec, lex], 10)
        chunk_id = next(iter(L.chunks))
        convo.add("turn-1", [("You can claim EUR 85 per day.", chunk_id, "D")])
        assert convo.render("turn-1", L, set())[0][1] == "EUR 85 per day"
        L.delete("D", 500)
        assert convo.render("turn-1", L, set())[0][1] == "[source removed]"
        PurgeWorker(L, stores).run(510)
        assert convo.render("turn-1", L, set())[0][0].startswith("[removed")
        assert Auditor(L, stores).run(900) == []

    @case("deleting during an in-flight ingest stops the late version from going live")
    def _():
        L, vec, lex, *_ = _world()
        L.create("D")
        v = L.register_upload("D", 0)
        L.delete("D", 5)
        assert not L.ingest_ready(v.version_id, ["x"], [vec, lex], 40)
        assert L.hydrate(vec.search(set()), set()) == []

    return t


def main() -> int:
    failed = 0
    tests = _tests()
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc!r}")
    print(f"\nledger.py: {len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
