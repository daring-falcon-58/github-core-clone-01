#!/usr/bin/env python3
"""makefun-forge: a standalone GitHub-compatible git forge (github-core-clone-01).

One process, Python 3.12/3.13 stdlib only. Serves:
  - git smart HTTP  at /{owner}/{repo}.git  (upload-pack protocol v2, receive-pack v0)
  - REST v3          under /api/v3
  - GraphQL v4      at /api/graphql
  - MCP             at /mcp  (JSON-RPC 2.0 over Streamable HTTP, revision 2026-07-28 + legacy)

Lifecycle: bind an ephemeral port, POST {"port": N} to $MAKEFUN_ENV_URL/register,
long-poll GET $MAKEFUN_ENV_URL/done until {"done": true}, then exit 0.
If MAKEFUN_ENV_URL is unset, serve standalone on 127.0.0.1:8080 (local dev/testing).

Determinism: no wall clock, no randomness in any state-affecting path. Server-created
commits use a fixed timestamp/identity. Two runs on the same instance leave identical state.
"""

import base64
import difflib
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- constants

ZERO_OID = "0" * 40
FIXED_TS = 1700000000
FIXED_TZ = "+0000"
IDENTITY = "Conformance <conformance@make.fun>"
AGENT = "makefun-forge/1.0"
FORGE_NAME = "makefun-forge"
FORGE_VERSION = "1.0.0"
FIXED_ISO = "2026-01-01T00:00:00Z"
DEFAULT_BRANCH = "main"
MAX_PACK_CACHE = 128

# ---------------------------------------------------------------- git objects

def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()

def hash_object(otype: str, data: bytes) -> str:
    return sha1_hex(("%s %d\0" % (otype, len(data))).encode() + data)

def parse_commit(data: bytes):
    """Return (tree, parents, author, committer, message). Message keeps raw bytes."""
    header, _, message = data.partition(b"\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in header.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
        elif line.startswith(b"author "):
            author = line[7:].decode(errors="replace")
        elif line.startswith(b"committer "):
            committer = line[10:].decode(errors="replace")
    return tree, parents, author, committer, message

def build_commit(tree, parents, message, author=IDENTITY, committer=IDENTITY,
                 timestamp=FIXED_TS, tz=FIXED_TZ):
    out = []
    out.append(("tree %s\n" % tree).encode())
    for p in parents:
        out.append(("parent %s\n" % p).encode())
    out.append(("author %s %d %s\n" % (author, timestamp, tz)).encode())
    out.append(("committer %s %d %s\n" % (committer, timestamp, tz)).encode())
    out.append(b"\n")
    if isinstance(message, str):
        message = message.encode()
    out.append(message)
    return b"".join(out)

def tree_sort_key(name: bytes, is_dir: bool):
    # git sorts tree entries by name, directories as if they end in '/'.
    return name + (b"/" if is_dir else b"")

def parse_tree(data: bytes):
    """Return list of (mode_str, name_str, oid_hex) in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode(errors="surrogateescape")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries

def build_tree(entries):
    """entries: list of (mode_str, name_str, oid_hex). Sorted git-style."""
    def key(e):
        mode, name, _ = e
        raw = name.encode("utf-8", "surrogateescape")
        return raw + (b"/" if mode == "40000" else b"")
    out = []
    for mode, name, oid in sorted(entries, key=key):
        out.append(("%s %s\0" % (mode, name)).encode("utf-8", "surrogateescape"))
        out.append(bytes.fromhex(oid))
    return b"".join(out)

def parse_tag(data: bytes):
    header, _, message = data.partition(b"\n\n")
    target = None
    ttype = None
    tag = ""
    tagger = ""
    for line in header.split(b"\n"):
        if line.startswith(b"object "):
            target = line[7:].decode()
        elif line.startswith(b"type "):
            ttype = line[5:].decode()
        elif line.startswith(b"tag "):
            tag = line[4:].decode(errors="replace")
        elif line.startswith(b"tagger "):
            tagger = line[7:].decode(errors="replace")
    return target, ttype, tag, tagger, message

def build_tag(target, ttype, name, message, tagger=IDENTITY,
              timestamp=FIXED_TS, tz=FIXED_TZ):
    out = []
    out.append(("object %s\n" % target).encode())
    out.append(("type %s\n" % ttype).encode())
    out.append(("tag %s\n" % name).encode())
    out.append(("tagger %s %d %s\n" % (tagger, timestamp, tz)).encode())
    out.append(b"\n")
    if isinstance(message, str):
        message = message.encode()
    out.append(message)
    return b"".join(out)

def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git binary delta to base bytes."""
    def read_varint(buf, pos):
        result = 0
        shift = 0
        while True:
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        return result, pos
    src_size, pos = read_varint(delta, 0)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    dst_size, pos = read_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != dst_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)

def read_pack(data: bytes):
    """Parse a packfile. Return (objects: {oid: (type, bytes)}, delta_count).
    Verifies the trailer SHA-1. Handles OFS_DELTA and REF_DELTA."""
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(data[8:12], "big")
    trailer = data[-20:]
    if sha1_hex(data[:-20]) != trailer.hex():
        raise ValueError("pack trailer checksum mismatch")
    pos = 12
    end = len(data) - 20
    objects = {}
    ofs_bases = {}  # offset -> (type, bytes)
    pending_ref = []  # (oid, base_oid, delta_bytes)
    delta_count = 0
    for _ in range(count):
        start = pos
        b = data[pos]
        pos += 1
        otype = (b >> 4) & 7
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = data[pos]
            pos += 1
            size |= (b & 0x7F) << shift
            shift += 7
        if otype == 6:  # OFS_DELTA
            b = data[pos]
            pos += 1
            off = b & 0x7F
            while b & 0x80:
                b = data[pos]
                pos += 1
                off = ((off + 1) << 7) | (b & 0x7F)
            base_off = start - off
            if base_off not in ofs_bases:
                raise ValueError("ofs-delta base not found")
            base_type, base_data = ofs_bases[base_off]
            do = zlib.decompressobj()
            raw = do.decompress(data[pos:end])
            consumed = len(data[pos:end]) - len(do.unused_data)
            pos += consumed
            resolved = apply_delta(base_data, raw)
            oid = hash_object(base_type, resolved)
            objects[oid] = (base_type, resolved)
            ofs_bases[start] = (base_type, resolved)
            delta_count += 1
        elif otype == 7:  # REF_DELTA
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            do = zlib.decompressobj()
            raw = do.decompress(data[pos:end])
            consumed = len(data[pos:end]) - len(do.unused_data)
            pos += consumed
            # store placeholder; resolve after all objects read
            pending_ref.append((base_oid, raw, start))
            delta_count += 1
        else:
            types = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
            tname = types.get(otype)
            if tname is None:
                raise ValueError("unknown pack object type %d" % otype)
            do = zlib.decompressobj()
            raw = do.decompress(data[pos:end])
            consumed = len(data[pos:end]) - len(do.unused_data)
            pos += consumed
            if len(raw) != size:
                raise ValueError("object size mismatch in pack")
            oid = hash_object(tname, raw)
            objects[oid] = (tname, raw)
            ofs_bases[start] = (tname, raw)
    # resolve REF_DELTAs (may chain)
    for _ in range(100):
        if not pending_ref:
            break
        remaining = []
        progressed = False
        for base_oid, raw, start in pending_ref:
            if base_oid in objects:
                base_type, base_data = objects[base_oid]
                resolved = apply_delta(base_data, raw)
                oid = hash_object(base_type, resolved)
                objects[oid] = (base_type, resolved)
                ofs_bases[start] = (base_type, resolved)
                progressed = True
            else:
                remaining.append((base_oid, raw, start))
        pending_ref = remaining
        if not progressed:
            break
    if pending_ref:
        raise ValueError("ref-delta base missing from pack")
    if pos != end:
        raise ValueError("pack length mismatch")
    return objects, delta_count

def write_pack(objects):
    """objects: list of (oid, type, bytes). Plain objects only, no deltas.
    Returns pack bytes with a correct SHA-1 trailer."""
    out = bytearray()
    out += b"PACK"
    out += (2).to_bytes(4, "big")
    out += len(objects).to_bytes(4, "big")
    type_codes = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
    for oid, otype, data in objects:
        code = type_codes[otype]
        size = len(data)
        header = bytearray()
        first = (code << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        header.append(first)
        while size:
            b = size & 0x7F
            size >>= 7
            if size:
                b |= 0x80
            header.append(b)
        out += bytes(header)
        out += zlib.compress(data, 6)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)

def reachable_objects(store, start_oids, stop_parents=None):
    """All object oids reachable from start_oids (commits, trees, tags, blobs).
    stop_parents: commits whose parents are NOT followed (shallow boundary —
    a shallow client holds the commit and its tree but not its history)."""
    seen = set()
    stack = list(start_oids)
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        entry = store.get(oid)
        if entry is None:
            continue
        seen.add(oid)
        otype, data = entry
        if otype == "commit":
            tree, parents, _, _, _ = parse_commit(data)
            if tree:
                stack.append(tree)
            if stop_parents is None or oid not in stop_parents:
                stack.extend(parents)
        elif otype == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    stack.append(child)
        elif otype == "tag":
            target, _, _, _, _ = parse_tag(data)
            if target:
                stack.append(target)
    return seen

def reachable_commits(store, start_oids, max_depth=None):
    """Commit oids reachable from start_oids, BFS by generation.
    max_depth: generations to include (1 = starts only). Returns (included, boundary)
    where boundary = included commits whose parent is not included."""
    included = []
    seen = set()
    frontier = [o for o in start_oids if o != ZERO_OID and o in store]
    depth = 0
    while frontier and (max_depth is None or depth < max_depth):
        nxt = []
        for oid in frontier:
            if oid in seen:
                continue
            seen.add(oid)
            included.append(oid)
            entry = store.get(oid)
            if entry and entry[0] == "commit":
                _, parents, _, _, _ = parse_commit(entry[1])
                for p in parents:
                    if p not in seen and p in store:
                        nxt.append(p)
        frontier = nxt
        depth += 1
    included_set = set(included)
    boundary = []
    for oid in included:
        entry = store.get(oid)
        if entry and entry[0] == "commit":
            _, parents, _, _, _ = parse_commit(entry[1])
            if any(p not in included_set for p in parents):
                boundary.append(oid)
    return included, boundary

def is_ancestor(store, ancestor, descendant):
    """True if ancestor is reachable from descendant (following parents)."""
    if ancestor == descendant:
        return True
    seen = set()
    stack = [descendant]
    while stack:
        oid = stack.pop()
        if oid == ancestor:
            return True
        if oid in seen:
            continue
        seen.add(oid)
        entry = store.get(oid)
        if entry and entry[0] == "commit":
            _, parents, _, _, _ = parse_commit(entry[1])
            stack.extend(parents)
    return False

def merge_base(store, a, b):
    """A common ancestor of a and b (first found by BFS from both), or None."""
    if a == b:
        return a
    anc_a = set()
    stack = [a]
    while stack:
        oid = stack.pop()
        if oid in anc_a or oid == ZERO_OID:
            continue
        anc_a.add(oid)
        entry = store.get(oid)
        if entry and entry[0] == "commit":
            _, parents, _, _, _ = parse_commit(entry[1])
            stack.extend(parents)
    stack = [b]
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in anc_a:
            return oid
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        entry = store.get(oid)
        if entry and entry[0] == "commit":
            _, parents, _, _, _ = parse_commit(entry[1])
            stack.extend(parents)
    return None

def flatten_tree(store, tree_oid):
    """{path: (mode, oid)} for every blob under a tree (recursive)."""
    out = {}
    def walk(toid, prefix):
        entry = store.get(toid)
        if not entry or entry[0] != "tree":
            return
        for mode, name, child in parse_tree(entry[1]):
            path = prefix + name if not prefix else prefix + "/" + name
            if mode == "40000":
                walk(child, path)
            elif mode == "160000":
                continue
            else:
                out[path] = (mode, child)
    walk(tree_oid, "")
    return out

def diff_trees(store, base_tree, head_tree):
    """List of (path, status, old_blob_oid|None, new_blob_oid|None) between two trees."""
    base = flatten_tree(store, base_tree) if base_tree else {}
    head = flatten_tree(store, head_tree) if head_tree else {}
    out = []
    for path in sorted(set(base) | set(head)):
        b = base.get(path)
        h = head.get(path)
        if b is None:
            out.append((path, "added", None, h[1]))
        elif h is None:
            out.append((path, "removed", b[1], None))
        elif b[1] != h[1]:
            out.append((path, "modified", b[1], h[1]))
    return out

def count_line_changes(store, old_oid, new_oid):
    """(additions, deletions) between two blobs via difflib."""
    def lines(oid):
        if oid is None:
            return []
        entry = store.get(oid)
        if not entry:
            return []
        return entry[1].decode("utf-8", "replace").splitlines()
    old = lines(old_oid)
    new = lines(new_oid)
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    add = 0
    dele = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            add += j2 - j1
        elif tag == "delete":
            dele += i2 - i1
        elif tag == "replace":
            add += j2 - j1
            dele += i2 - i1
    return add, dele

# ---------------------------------------------------------------- pkt-line

def pkt_line(data: bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return ("%04x" % (len(data) + 4)).encode() + data

PKT_FLUSH = b"0000"
PKT_DELIM = b"0001"
PKT_RESPONSE_END = b"0002"

def parse_pkt_lines(body: bytes):
    """Yield ('data', payload) / ('flush', None) / ('delim', None) / ('response-end', None).
    Returns (packets, rest_bytes) where rest_bytes is everything after the first flush
    (used by receive-pack: the pack follows the command flush)."""
    packets = []
    pos = 0
    n = len(body)
    while pos + 4 <= n:
        try:
            ln = int(body[pos:pos + 4], 16)
        except ValueError:
            break
        if ln == 0:
            packets.append(("flush", None))
            return packets, body[pos + 4:]
        if ln == 1:
            packets.append(("delim", None))
            pos += 4
            continue
        if ln == 2:
            packets.append(("response-end", None))
            pos += 4
            continue
        if ln < 4 or pos + ln > n:
            break
        packets.append(("data", body[pos + 4:pos + ln]))
        pos += ln
    return packets, b""

def sideband_packets(pack: bytes):
    """Split pack bytes into sideband band-1 pkt-lines (<=65515 payload bytes each)."""
    out = []
    i = 0
    while i < len(pack):
        chunk = pack[i:i + 65515]
        out.append(pkt_line(b"\x01" + chunk))
        i += len(chunk)
    return out

# ---------------------------------------------------------------- global ids

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def global_id(kind, *parts):
    return b64url(("%s:%s" % (kind, ":".join(str(p) for p in parts))).encode())

def decode_global_id(gid):
    try:
        padded = gid + "=" * (-len(gid) % 4)
        text = base64.urlsafe_b64decode(padded.encode()).decode()
        kind, _, rest = text.partition(":")
        return kind, rest
    except Exception:
        return None, None

# ---------------------------------------------------------------- store

class Repo:
    __slots__ = ("name", "owner", "private", "description", "default_branch",
                 "refs", "issues", "pulls", "labels", "comments",
                 "issue_seq", "pr_seq", "comment_seq", "database_id",
                 "seq", "pack_cache")

    def __init__(self, name, owner, private, description, database_id, seq):
        self.name = name
        self.owner = owner
        self.private = private
        self.description = description
        self.default_branch = DEFAULT_BRANCH
        self.refs = {}          # refname -> oid
        self.issues = {}        # number -> dict
        self.pulls = {}         # number -> dict
        self.labels = {}        # name -> dict
        self.comments = {}      # issue number -> [comment dict]
        self.issue_seq = 0      # shared issue+PR number counter
        self.pr_seq = 0
        self.comment_seq = 0
        self.database_id = database_id
        self.seq = seq
        self.pack_cache = {}    # fetch cache key -> (pack_bytes, shallow, unshallow)

def repo_owner(repo):
    return repo.owner or STORE.owner_login or ""

def repo_gid(repo):
    return global_id("Repository", repo_owner(repo), repo.name)

def issue_gid(repo, number):
    return global_id("Issue", repo_owner(repo), repo.name, number)

def pr_gid(repo, number):
    return global_id("PullRequest", repo_owner(repo), repo.name, number)

def comment_gid(repo, issue_number, cid):
    return global_id("IssueComment", repo_owner(repo), repo.name, issue_number, cid)

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.objects = {}       # oid -> (type, bytes)
        self.repos = {}         # name -> Repo
        self.owner_login = None  # learned lazily from the first owner seen in a URL
        self.repo_seq = 0
        self.db_seq = 0
        self.req_seq = 0
        self.user_id = 1000

    # -- owner binding -------------------------------------------------
    def bind_owner(self, owner):
        """Learn the token owner's login from the first owner seen; return the
        canonical owner for this token (the bound one once known)."""
        if owner is None:
            return self.owner_login
        if self.owner_login is None:
            self.owner_login = owner
            for repo in self.repos.values():
                if repo.owner is None:
                    repo.owner = owner
        return self.owner_login

    def is_owner(self, owner):
        """True if `owner` is the token owner's login (binds it if unknown)."""
        if owner is None:
            return False
        if self.owner_login is None:
            self.bind_owner(owner)
            return True
        return owner == self.owner_login

    def get_repo(self, owner, name):
        """Repo by (owner, name); None when the owner is foreign or it is missing."""
        if owner is None or name is None:
            return None
        if not self.is_owner(owner):
            return None
        return self.repos.get(name)

    def create_repo(self, name, private, description):
        if name in self.repos:
            return None
        self.repo_seq += 1
        self.db_seq += 1
        owner = self.owner_login  # may be None until first URL binds it
        repo = Repo(name, owner, private, description, self.db_seq, self.repo_seq)
        self.repos[name] = repo
        return repo

    # -- objects -------------------------------------------------------
    def put_object(self, otype, data):
        oid = hash_object(otype, data)
        self.objects[oid] = (otype, data)
        return oid

    def next_req_id(self):
        self.req_seq += 1
        return "req-%d" % self.req_seq

STORE = Store()

# ---------------------------------------------------------------- repo helpers

def repo_tip(repo, branch=None):
    ref = "refs/heads/%s" % (branch or repo.default_branch)
    return repo.refs.get(ref)

def repo_json(repo):
    owner = repo_owner(repo)
    return {
        "id": repo_gid(repo),
        "node_id": repo_gid(repo),
        "databaseId": repo.database_id,
        "name": repo.name,
        "full_name": "%s/%s" % (owner, repo.name),
        "owner": {"login": owner, "id": STORE.user_id, "type": "User"},
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "created_at": FIXED_ISO,
        "updated_at": FIXED_ISO,
        "pushed_at": FIXED_ISO,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "has_pages": False,
        "has_downloads": True,
        "archived": False,
        "disabled": False,
        "fork": False,
        "url": "/repos/%s/%s" % (owner, repo.name),
        "html_url": "https://example.invalid/%s/%s" % (owner, repo.name),
    }

def issue_json(repo, issue):
    return {
        "id": issue_gid(repo, issue["number"]),
        "node_id": issue_gid(repo, issue["number"]),
        "number": issue["number"],
        "title": issue["title"],
        "body": issue.get("body"),
        "state": issue["state"],
        "labels": [{"name": n} for n in issue.get("labels", [])],
        "user": {"login": repo.owner or STORE.owner_login or "", "id": STORE.user_id, "type": "User"},
        "created_at": FIXED_ISO,
        "updated_at": FIXED_ISO,
        "comments": len(repo.comments.get(issue["number"], [])),
    }

def pull_json(repo, pr):
    return {
        "id": pr_gid(repo, pr["number"]),
        "node_id": pr_gid(repo, pr["number"]),
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body"),
        "state": pr["state"],
        "merged": pr["merged"],
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "head": {"ref": pr["head"], "sha": pr["head_sha"],
                 "label": "%s:%s" % (repo.owner or STORE.owner_login or "", pr["head"])},
        "base": {"ref": pr["base"], "sha": pr["base_sha"],
                 "label": "%s:%s" % (repo.owner or STORE.owner_login or "", pr["base"])},
        "user": {"login": repo.owner or STORE.owner_login or "", "id": STORE.user_id, "type": "User"},
        "created_at": FIXED_ISO,
        "updated_at": FIXED_ISO,
    }

def commit_message_of(oid):
    entry = STORE.objects.get(oid)
    if not entry or entry[0] != "commit":
        return ""
    _, _, _, _, message = parse_commit(entry[1])
    return message.decode("utf-8", "replace").rstrip("\n")

def list_branch_commits(repo, branch):
    """Commit oids reachable from the branch tip, newest first (insertion order)."""
    tip = repo_tip(repo, branch)
    if not tip:
        return []
    # order by reachability BFS then by insertion (objects were stored in push order;
    # use a sequence counter on first sight instead: walk parents depth-first).
    order = []
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        order.append(oid)
        entry = STORE.objects.get(oid)
        if entry and entry[0] == "commit":
            _, parents, _, _, _ = parse_commit(entry[1])
            stack.extend(reversed(parents))
    return order

def build_tree_from_files(store, base_tree_oid, files):
    """files: {path: blob_oid or None (delete)}. Returns new tree oid.
    Builds nested trees from the base tree's flattened files."""
    flat = flatten_tree(store, base_tree_oid) if base_tree_oid else {}
    for path, oid in files.items():
        if oid is None:
            flat.pop(path, None)
        else:
            flat[path] = ("100644", oid)
    # group by directory
    def build(prefix):
        entries = []
        subdirs = {}
        for path in sorted(flat):
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1:] if prefix else path
            if "/" in rest:
                d = rest.split("/", 1)[0]
                subdirs.setdefault(d, True)
            else:
                mode, oid = flat[path]
                entries.append((mode, rest, oid))
        for d in sorted(subdirs):
            sub_oid = build(prefix + "/" + d if prefix else d)
            entries.append(("40000", d, sub_oid))
        data = build_tree(entries)
        return store_put_tree(data)
    # store_put_tree stores the tree object and returns its oid
    return build("")

def store_put_tree(data):
    return STORE.put_object("tree", data)

def make_commit(repo, branch, files, message):
    """Create a commit on `branch` applying `files` ({path: blob_oid|None}).
    Returns (commit_oid, tree_oid). Creates the branch if missing (parent =
    default branch tip when one exists)."""
    tip = repo_tip(repo, branch)
    base_tree = None
    parents = []
    if tip:
        parents = [tip]
        entry = STORE.objects.get(tip)
        if entry and entry[0] == "commit":
            base_tree, _, _, _, _ = parse_commit(entry[1])
    else:
        default_tip = repo_tip(repo, None)
        if branch != repo.default_branch and default_tip:
            parents = [default_tip]
            entry = STORE.objects.get(default_tip)
            if entry and entry[0] == "commit":
                base_tree, _, _, _, _ = parse_commit(entry[1])
    tree_oid = build_tree_from_files(STORE.objects, base_tree, files)
    commit_data = build_commit(tree_oid, parents, message)
    commit_oid = STORE.put_object("commit", commit_data)
    repo.refs["refs/heads/%s" % branch] = commit_oid
    return commit_oid, tree_oid

def pr_files(repo, pr):
    """Diff (merge-base -> head) file list for a pull request."""
    base_tip = repo_tip(repo, pr["base"])
    head_tip = repo_tip(repo, pr["head"])
    if not head_tip:
        return []
    base_tree = None
    if base_tip:
        mb = merge_base(STORE.objects, base_tip, head_tip)
        if mb:
            entry = STORE.objects.get(mb)
            if entry and entry[0] == "commit":
                base_tree, _, _, _, _ = parse_commit(entry[1])
    head_entry = STORE.objects.get(head_tip)
    head_tree = None
    if head_entry and head_entry[0] == "commit":
        head_tree, _, _, _, _ = parse_commit(head_entry[1])
    out = []
    for path, status, old_oid, new_oid in diff_trees(STORE.objects, base_tree, head_tree):
        add, dele = count_line_changes(STORE.objects, old_oid, new_oid)
        if status == "added":
            add = max(add, 1)
        out.append({
            "filename": path,
            "status": status,
            "additions": add,
            "deletions": dele,
            "changes": add + dele,
        })
    return out

def pr_mergeable(repo, pr):
    """MERGEABLE unless head and base both changed the same file differently."""
    base_tip = repo_tip(repo, pr["base"])
    head_tip = repo_tip(repo, pr["head"])
    if not base_tip or not head_tip:
        return "UNKNOWN"
    mb = merge_base(STORE.objects, base_tip, head_tip)
    if not mb:
        return "CONFLICTING"
    def tree_of(tip):
        entry = STORE.objects.get(tip)
        if entry and entry[0] == "commit":
            return parse_commit(entry[1])[0]
        return None
    mb_tree = tree_of(mb)
    base_tree = tree_of(base_tip)
    head_tree = tree_of(head_tip)
    mb_files = flatten_tree(STORE.objects, mb_tree) if mb_tree else {}
    base_files = flatten_tree(STORE.objects, base_tree) if base_tree else {}
    head_files = flatten_tree(STORE.objects, head_tree) if head_tree else {}
    for path in set(head_files) | set(base_files):
        mb_oid = mb_files.get(path, (None, None))[1]
        base_oid = base_files.get(path, (None, None))[1]
        head_oid = head_files.get(path, (None, None))[1]
        changed_head = head_oid != mb_oid
        changed_base = base_oid != mb_oid
        if changed_head and changed_base and head_oid != base_oid:
            return "CONFLICTING"
    return "MERGEABLE"

def squash_merge(repo, pr):
    """Squash-merge pr into its base branch. Returns (commit_oid, conflict_bool)."""
    base_tip = repo_tip(repo, pr["base"])
    head_tip = repo_tip(repo, pr["head"])
    if not base_tip or not head_tip:
        return None, True
    if pr_mergeable(repo, pr) == "CONFLICTING":
        return None, True
    mb = merge_base(STORE.objects, base_tip, head_tip)
    def tree_of(tip):
        entry = STORE.objects.get(tip)
        if entry and entry[0] == "commit":
            return parse_commit(entry[1])[0]
        return None
    mb_tree = tree_of(mb)
    base_tree = tree_of(base_tip)
    head_tree = tree_of(head_tip)
    mb_files = flatten_tree(STORE.objects, mb_tree) if mb_tree else {}
    head_files = flatten_tree(STORE.objects, head_tree) if head_tree else {}
    # result tree = base tree with the (merge-base -> head) diff applied
    files = {}
    for path, (mode, oid) in head_files.items():
        if mb_files.get(path, (None, None))[1] != oid:
            files[path] = oid
    for path in mb_files:
        if path not in head_files:
            files[path] = None
    tree_oid = build_tree_from_files(STORE.objects, base_tree, files)
    message = "Merge pull request #%d: %s\n" % (pr["number"], pr["title"])
    commit_data = build_commit(tree_oid, [base_tip], message)
    commit_oid = STORE.put_object("commit", commit_data)
    repo.refs["refs/heads/%s" % pr["base"]] = commit_oid
    pr["merged"] = True
    pr["state"] = "closed"
    pr["merged_at"] = FIXED_ISO
    pr["merge_commit_sha"] = commit_oid
    return commit_oid, False

# ---------------------------------------------------------------- git HTTP

def git_refs_sorted(repo):
    """All refs as (refname, oid) sorted by refname, HEAD first when present."""
    out = []
    tip = repo_tip(repo, None)
    if tip:
        out.append(("HEAD", tip))
    for ref in sorted(repo.refs):
        out.append((ref, repo.refs[ref]))
    return out

def v2_capabilities():
    return ["agent=%s" % AGENT, "object-format=sha1", "ls-refs", "fetch=shallow"]

def v0_upload_capabilities():
    return ["symref=HEAD:refs/heads/%s" % DEFAULT_BRANCH, "side-band-64k",
            "thin-pack", "ofs-delta", "multi_ack_detailed", "shallow",
            "agent=%s" % AGENT, "object-format=sha1"]

def v0_receive_capabilities():
    return ["report-status", "side-band-64k", "delete-refs", "ofs-delta",
            "agent=%s" % AGENT, "object-format=sha1"]

def advertisement(repo, service, proto2):
    """info/refs advertisement bytes."""
    out = []
    if service == "git-upload-pack":
        if proto2:
            out.append(pkt_line("# service=git-upload-pack\n"))
            out.append(PKT_FLUSH)
            out.append(pkt_line("version 2\n"))
            for cap in v2_capabilities():
                out.append(pkt_line(cap + "\n"))
            out.append(PKT_FLUSH)
            return b"".join(out)
        caps = " ".join(v0_upload_capabilities())
        out.append(pkt_line("# service=git-upload-pack\n"))
        out.append(PKT_FLUSH)
        refs = git_refs_sorted(repo)
        if not refs:
            out.append(pkt_line("%s capabilities^{}\0%s\n" % (ZERO_OID, caps)))
        else:
            first = True
            for name, oid in refs:
                if first:
                    out.append(pkt_line("%s %s\0%s\n" % (oid, name, caps)))
                    first = False
                else:
                    out.append(pkt_line("%s %s\n" % (oid, name)))
            # peeled tag lines
            for name, oid in refs:
                if name.startswith("refs/tags/"):
                    entry = STORE.objects.get(oid)
                    if entry and entry[0] == "tag":
                        target, _, _, _, _ = parse_tag(entry[1])
                        if target:
                            out.append(pkt_line("%s %s^{}\n" % (target, name)))
        out.append(PKT_FLUSH)
        return b"".join(out)
    # receive-pack, v0 only
    caps = " ".join(v0_receive_capabilities())
    out.append(pkt_line("# service=git-receive-pack\n"))
    out.append(PKT_FLUSH)
    refs = git_refs_sorted(repo)
    if not refs:
        out.append(pkt_line("%s capabilities^{}\0%s\n" % (ZERO_OID, caps)))
    else:
        first = True
        for name, oid in refs:
            if first:
                out.append(pkt_line("%s %s\0%s\n" % (oid, name, caps)))
                first = False
            else:
                out.append(pkt_line("%s %s\n" % (oid, name)))
        for name, oid in refs:
            if name.startswith("refs/tags/"):
                entry = STORE.objects.get(oid)
                if entry and entry[0] == "tag":
                    target, _, _, _, _ = parse_tag(entry[1])
                    if target:
                        out.append(pkt_line("%s %s^{}\n" % (target, name)))
    out.append(PKT_FLUSH)
    return b"".join(out)

def ls_refs_response(repo, prefixes):
    """v2 ls-refs answer: one pkt-line per ref + flush."""
    out = []
    tip = repo_tip(repo, None)
    lines = []
    if tip:
        lines.append("%s HEAD symref-target:refs/heads/%s\n" % (tip, repo.default_branch))
    else:
        lines.append("%s HEAD symref-target:refs/heads/%s\n" % (ZERO_OID, repo.default_branch))
    for ref in sorted(repo.refs):
        oid = repo.refs[ref]
        lines.append("%s %s\n" % (oid, ref))
        entry = STORE.objects.get(oid)
        if ref.startswith("refs/tags/") and entry and entry[0] == "tag":
            target, _, _, _, _ = parse_tag(entry[1])
            if target:
                lines[-1] = "%s %s peeled:%s\n" % (oid, ref, target)
    for line in lines:
        name = line.split(" ", 1)[1].split(" ", 1)[0]
        if not prefixes or any(name.startswith(p) for p in prefixes):
            out.append(pkt_line(line))
    out.append(PKT_FLUSH)
    return b"".join(out)

def empty_pack():
    """A packfile with zero objects (valid PACK v2 + trailer)."""
    header = b"PACK" + (2).to_bytes(4, "big") + (0).to_bytes(4, "big")
    return header + hashlib.sha1(header).digest()

def fetch_pack(repo, wants, haves, depth, old_shallow):
    """Compute the fetch. Returns (pack_bytes|None, shallow, unshallow).
    Cached on (repo name, wants, haves, depth, old_shallow)."""
    key = (repo.name, tuple(sorted(wants)), tuple(sorted(haves)),
           depth, tuple(sorted(old_shallow)))
    cached = repo.pack_cache.get(key)
    if cached is not None:
        return cached
    if depth is not None:
        included, boundary = reachable_commits(STORE.objects, wants, max_depth=depth)
    else:
        included, boundary = reachable_commits(STORE.objects, wants, max_depth=None)
    # objects reachable from included commits' trees
    wanted_objs = set()
    for oid in included:
        wanted_objs.add(oid)
        entry = STORE.objects.get(oid)
        if entry and entry[0] == "commit":
            tree, _, _, _, _ = parse_commit(entry[1])
            if tree:
                wanted_objs |= reachable_objects(STORE.objects, [tree])
    # A shallow client's haves do not imply their parents: stop the
    # have-closure at the client's shallow boundary.
    have_objs = (reachable_objects(STORE.objects, haves,
                                   stop_parents=set(old_shallow))
                 if haves else set())
    send = wanted_objs - have_objs
    # order: commits in BFS order, then the rest in first-seen DFS order
    ordered = []
    seen = set()
    for oid in included:
        if oid in send and oid not in seen:
            seen.add(oid)
            ordered.append(oid)
    def add_tree(toid):
        entry = STORE.objects.get(toid)
        if not entry or entry[0] != "tree":
            return
        if toid in send and toid not in seen:
            seen.add(toid)
            ordered.append(toid)
        for mode, _name, child in parse_tree(entry[1]):
            if mode == "40000":
                add_tree(child)
            elif mode != "160000" and child in send and child not in seen:
                seen.add(child)
                ordered.append(child)
    for oid in included:
        entry = STORE.objects.get(oid)
        if entry and entry[0] == "commit":
            tree, _, _, _, _ = parse_commit(entry[1])
            if tree:
                add_tree(tree)
    # any remaining send objects (tags etc.) in sorted order
    for oid in sorted(send - seen):
        ordered.append(oid)
    pack = None
    if ordered:
        pack = write_pack([(oid, STORE.objects[oid][0], STORE.objects[oid][1])
                           for oid in ordered])
    shallow = boundary
    unshallow = [o for o in old_shallow if o in set(included)]
    result = (pack, shallow, unshallow)
    if len(repo.pack_cache) >= MAX_PACK_CACHE:
        repo.pack_cache.clear()
    repo.pack_cache[key] = result
    return result

def handle_fetch_v2(repo, body):
    """v2 fetch / ls-refs request. Returns response bytes."""
    packets, _ = parse_pkt_lines(body)
    command = None
    wants = []
    haves = []
    shallow = []
    depth = None
    prefixes = []
    in_args = True
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind == "delim":
            in_args = False
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("command="):
            command = line[len("command="):]
        elif line.startswith("want "):
            wants.append(line[5:].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
        elif line.startswith("ref-prefix "):
            prefixes.append(line[len("ref-prefix "):])
        # thin-pack, no-progress, ofs-delta, filter, peel, symrefs, unborn, done: ignored
    if command == "ls-refs":
        return ls_refs_response(repo, prefixes)
    if command != "fetch":
        raise BadRequest("unknown git command")
    pack, new_shallow, unshallow = fetch_pack(repo, wants, haves, depth, shallow)
    out = []
    # The acknowledgments section is only sent when the client negotiated
    # (sent 'have' lines). A fresh fetch with no haves goes straight to
    # shallow-info / packfile; real git rejects an acks section there.
    if haves:
        out.append(pkt_line("acknowledgments\n"))
        if pack is None:
            # nothing new: ACK the wants we have + ready
            for w in wants:
                if w in haves or w in reachable_objects(STORE.objects, haves):
                    out.append(pkt_line("ACK %s\n" % w))
            out.append(pkt_line("ready\n"))
        else:
            # incremental: ACK the common commits
            for h in haves:
                out.append(pkt_line("ACK %s\n" % h))
            out.append(pkt_line("ready\n"))
        out.append(PKT_DELIM)
    if new_shallow or unshallow:
        out.append(pkt_line("shallow-info\n"))
        for o in new_shallow:
            out.append(pkt_line("shallow %s\n" % o))
        for o in unshallow:
            out.append(pkt_line("unshallow %s\n" % o))
        out.append(PKT_DELIM)
    if pack is not None:
        out.append(pkt_line("packfile\n"))
        out.extend(sideband_packets(pack))
    elif wants:
        # Nothing to send (empty repo, nothing-new, or a deepen with no new
        # generation): answer an empty packfile so the client sees 0 objects
        # instead of a bare flush it cannot parse.
        out.append(pkt_line("packfile\n"))
        out.extend(sideband_packets(empty_pack()))
    out.append(PKT_FLUSH)
    return b"".join(out)

def handle_fetch_v0(repo, body):
    """v0 upload-pack fetch (fallback; this client uses v2, but implement it)."""
    packets, _ = parse_pkt_lines(body)
    wants = []
    haves = []
    shallow = []
    depth = None
    seen_flush = False
    for kind, payload in packets:
        if kind == "flush":
            if not seen_flush:
                seen_flush = True
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            wants.append(line[5:].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
    pack, new_shallow, unshallow = fetch_pack(repo, wants, haves, depth, shallow)
    out = []
    if haves and pack is None:
        for w in wants:
            out.append(pkt_line("ACK %s\n" % w))
        out.append(pkt_line("ready\n"))
    elif haves:
        for h in haves:
            out.append(pkt_line("ACK %s\n" % h))
        out.append(pkt_line("ready\n"))
    else:
        out.append(pkt_line("NAK\n"))
    for o in new_shallow:
        out.append(pkt_line("shallow %s\n" % o))
    for o in unshallow:
        out.append(pkt_line("unshallow %s\n" % o))
    if pack is not None:
        out.extend(sideband_packets(pack))
    return b"".join(out)

def handle_receive_pack(repo, body):
    """v0 receive-pack. Returns (status, response_bytes)."""
    packets, pack_bytes = parse_pkt_lines(body)
    commands = []
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace")
        # first line carries capabilities after NUL
        if "\0" in line:
            line = line.split("\0", 1)[0]
        line = line.rstrip("\n")
        parts = line.split(" ")
        if len(parts) >= 3:
            commands.append((parts[0], parts[1], parts[2]))
    # store the pack first
    unpack_ok = True
    unpack_detail = ""
    if pack_bytes:
        try:
            objects, _delta = read_pack(pack_bytes)
            for oid, (otype, data) in objects.items():
                STORE.objects[oid] = (otype, data)
        except Exception as exc:
            unpack_ok = False
            unpack_detail = str(exc)
    results = []
    if unpack_ok:
        for old, new, ref in commands:
            if new == ZERO_OID:
                # delete
                if ref in repo.refs:
                    del repo.refs[ref]
                    results.append((ref, True, ""))
                else:
                    results.append((ref, True, ""))
                continue
            if old == ZERO_OID:
                # create
                if ref in repo.refs:
                    results.append((ref, False, "already exists"))
                    continue
                if new not in STORE.objects:
                    results.append((ref, False, "object not found"))
                    continue
                repo.refs[ref] = new
                results.append((ref, True, ""))
                continue
            # update
            current = repo.refs.get(ref)
            if current != old:
                results.append((ref, False, "non-fast-forward"))
                continue
            if new not in STORE.objects:
                results.append((ref, False, "object not found"))
                continue
            if not is_ancestor(STORE.objects, old, new):
                results.append((ref, False, "non-fast-forward"))
                continue
            repo.refs[ref] = new
            results.append((ref, True, ""))
    out = []
    if unpack_ok:
        out.append(pkt_line(b"\x01" + b"unpack ok\n"))
    else:
        out.append(pkt_line(b"\x01" + ("unpack %s\n" % unpack_detail).encode()))
    for ref, ok, reason in results:
        if ok:
            out.append(pkt_line(b"\x01" + ("ok %s\n" % ref).encode()))
        else:
            out.append(pkt_line(b"\x01" + ("ng %s %s\n" % (ref, reason)).encode()))
    out.append(PKT_FLUSH)
    return b"".join(out)

# ---------------------------------------------------------------- errors

class BadRequest(Exception):
    pass

class NotFound(Exception):
    pass

class Unprocessable(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

class Conflict(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

class MethodNotAllowed(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

# ---------------------------------------------------------------- REST v3

def parse_pagination(query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    return per_page, page

def page_link(host, path, query, per_page, page, total):
    """Link header for pagination (same-origin absolute URL)."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page >= total_pages:
        return None
    q = dict(query)
    q["per_page"] = [str(per_page)]
    q["page"] = [str(page + 1)]
    qs = urllib.parse.urlencode([(k, v) for k, vals in q.items() for v in vals])
    url = "http://%s%s?%s" % (host, path, qs)
    links = ['<%s>; rel="next"' % url]
    q["page"] = [str(total_pages)]
    qs = urllib.parse.urlencode([(k, v) for k, vals in q.items() for v in vals])
    links.append('<%s>; rel="last"' % ("http://%s%s?%s" % (host, path, qs)))
    return ", ".join(links)

def paginate(items, query):
    per_page, page = parse_pagination(query)
    start = (page - 1) * per_page
    return items[start:start + per_page], per_page, page, len(items)

def rest_list_repos(host, path, query):
    owner = STORE.owner_login or ""
    names = sorted(STORE.repos)
    items, per_page, page, total = paginate(
        [repo_json(STORE.repos[n]) for n in names], query)
    link = page_link(host, path, query, per_page, page, total)
    return 200, items, {"Link": link} if link else {}

def rest_create_repo(body):
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise Unprocessable("name is required")
    if name in STORE.repos:
        raise Unprocessable("Repository %s already exists" % name)
    repo = STORE.create_repo(name, bool(body.get("private", False)),
                             body.get("description"))
    return 201, repo_json(repo), {}

def rest_get_repo(owner, name):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    return 200, repo_json(repo), {}

def rest_patch_repo(owner, name, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    if "description" in body:
        repo.description = body.get("description")
    if "private" in body:
        repo.private = bool(body.get("private"))
    return 200, repo_json(repo), {}

def rest_delete_repo(owner, name):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    del STORE.repos[name]
    return 204, None, {}

def rest_list_branches(owner, name):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    out = [{"name": ref[len("refs/heads/"):], "commit": {"sha": oid}}
           for ref, oid in sorted(repo.refs.items())
           if ref.startswith("refs/heads/")]
    return 200, out, {}

def rest_get_branch(owner, name, branch):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    oid = repo.refs.get("refs/heads/%s" % branch)
    if not oid:
        raise NotFound("Branch not found")
    return 200, {"name": branch, "commit": {"sha": oid}}, {}

def rest_get_ref(owner, name, refpath):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    ref = "refs/%s" % refpath
    oid = repo.refs.get(ref)
    if not oid:
        raise NotFound("Reference not found")
    return 200, {"ref": ref, "object": {"sha": oid, "type": "commit"}}, {}

def rest_create_ref(owner, name, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    ref = body.get("ref")
    sha = body.get("sha")
    if not ref or not sha:
        raise Unprocessable("ref and sha are required")
    if ref in repo.refs:
        raise Unprocessable("Reference %s already exists" % ref)
    if sha not in STORE.objects:
        raise Unprocessable("Object %s does not exist" % sha)
    repo.refs[ref] = sha
    return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, {}

def rest_list_commits(owner, name, query):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    branch = query.get("sha", [None])[0]
    commits = list_branch_commits(repo, branch or repo.default_branch)
    out = [{"sha": oid, "commit": {"message": commit_message_of(oid)}}
           for oid in commits]
    return 200, out, {}

def _contents_tree(repo, branch):
    tip = repo_tip(repo, branch)
    if not tip:
        return None
    entry = STORE.objects.get(tip)
    if not entry or entry[0] != "commit":
        return None
    return parse_commit(entry[1])[0]

def rest_put_contents(owner, name, path, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    message = body.get("message") or "Update %s" % path
    content_b64 = body.get("content")
    if content_b64 is None:
        raise Unprocessable("content is required")
    try:
        raw = base64.b64decode(content_b64, validate=False)
    except Exception:
        raise BadRequest("content is not valid base64")
    branch = body.get("branch") or repo.default_branch
    sha = body.get("sha")
    tree_oid = _contents_tree(repo, branch)
    flat = flatten_tree(STORE.objects, tree_oid) if tree_oid else {}
    existing = flat.get(path)
    if existing is not None:
        if not sha or sha != existing[1]:
            raise Conflict("sha does not match the current file (stale sha)")
        status = 200
    else:
        if sha:
            raise Conflict("sha does not match the current file (stale sha)")
        status = 201
    blob_oid = STORE.put_object("blob", raw)
    commit_oid, _ = make_commit(repo, branch, {path: blob_oid}, message)
    return status, {"content": {"sha": blob_oid, "encoding": "base64"},
                    "commit": {"sha": commit_oid}}, {}

def rest_get_contents(owner, name, path, query):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    branch = query.get("ref", [None])[0] or repo.default_branch
    tree_oid = _contents_tree(repo, branch)
    if tree_oid is None:
        raise NotFound("Not Found")
    flat = flatten_tree(STORE.objects, tree_oid)
    if path in flat:
        mode, blob_oid = flat[path]
        entry = STORE.objects.get(blob_oid)
        raw = entry[1] if entry else b""
        return 200, {"type": "file", "encoding": "base64",
                     "content": base64.b64encode(raw).decode(),
                     "sha": blob_oid, "size": len(raw),
                     "name": path.rsplit("/", 1)[-1], "path": path}, {}
    # directory?
    prefix = path + "/" if path else ""
    entries = {}
    for p, (mode, oid) in flat.items():
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if "/" in rest:
            d = rest.split("/", 1)[0]
            entries.setdefault(d, ("dir", None))
        else:
            entries[rest] = ("file", oid)
    if not entries:
        raise NotFound("Not Found")
    out = []
    for entry_name in sorted(entries):
        kind, oid = entries[entry_name]
        if kind == "dir":
            # tree oid for the directory
            dir_oid = _dir_tree_oid(repo, branch, prefix + entry_name)
            out.append({"type": "dir", "name": entry_name,
                        "path": prefix + entry_name, "sha": dir_oid or ""})
        else:
            out.append({"type": "file", "name": entry_name,
                        "path": prefix + entry_name, "sha": oid})
    return 200, out, {}

def _dir_tree_oid(repo, branch, dirpath):
    """Tree oid of a directory inside the branch tip tree (or '')."""
    tree_oid = _contents_tree(repo, branch)
    if not tree_oid:
        return ""
    parts = [p for p in dirpath.split("/") if p]
    cur = tree_oid
    for part in parts:
        entry = STORE.objects.get(cur)
        if not entry or entry[0] != "tree":
            return ""
        nxt = None
        for mode, name, child in parse_tree(entry[1]):
            if name == part and mode == "40000":
                nxt = child
                break
        if nxt is None:
            return ""
        cur = nxt
    return cur

def rest_delete_contents(owner, name, path, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    message = body.get("message") or "Delete %s" % path
    sha = body.get("sha")
    branch = body.get("branch") or repo.default_branch
    tree_oid = _contents_tree(repo, branch)
    flat = flatten_tree(STORE.objects, tree_oid) if tree_oid else {}
    existing = flat.get(path)
    if existing is None:
        raise NotFound("Not Found")
    if sha and sha != existing[1]:
        raise Conflict("sha does not match the current file (stale sha)")
    commit_oid, _ = make_commit(repo, branch, {path: None}, message)
    return 200, {"commit": {"sha": commit_oid}}, {}

def rest_create_label(owner, name, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    label = body.get("name")
    if not label:
        raise Unprocessable("name is required")
    repo.labels[label] = {"name": label, "color": body.get("color", "")}
    return 201, {"name": label}, {}

def rest_create_issue(owner, name, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    title = body.get("title")
    if not title:
        raise Unprocessable("title is required")
    labels = body.get("labels") or []
    for lab in labels:
        lab_name = lab if isinstance(lab, str) else lab.get("name")
        if lab_name not in repo.labels:
            raise Unprocessable("Label %s does not exist" % lab_name)
    repo.issue_seq += 1
    number = repo.issue_seq
    issue = {"number": number, "title": title, "body": body.get("body"),
             "state": "open",
             "labels": [l if isinstance(l, str) else l.get("name") for l in labels]}
    repo.issues[number] = issue
    return 201, issue_json(repo, issue), {}

def rest_list_issues(owner, name, query):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    state = query.get("state", ["open"])[0]
    direction = query.get("direction", ["desc"])[0]
    issues = list(repo.issues.values())
    if state == "open":
        issues = [i for i in issues if i["state"] == "open"]
    elif state == "closed":
        issues = [i for i in issues if i["state"] == "closed"]
    issues.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
    items, per_page, page, total = paginate([issue_json(repo, i) for i in issues], query)
    return 200, items, {}

def rest_get_issue(owner, name, number):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise NotFound("Not Found")
    return 200, issue_json(repo, issue), {}

def rest_patch_issue(owner, name, number, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise NotFound("Not Found")
    if "state" in body:
        issue["state"] = body["state"]
    if "title" in body:
        issue["title"] = body["title"]
    if "body" in body:
        issue["body"] = body["body"]
    return 200, issue_json(repo, issue), {}

def rest_create_comment(owner, name, number, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise NotFound("Not Found")
    text = body.get("body")
    if text is None:
        raise Unprocessable("body is required")
    repo.comment_seq += 1
    cid = repo.comment_seq
    comment = {"id": cid, "body": text}
    repo.comments.setdefault(number, []).append(comment)
    return 201, {"id": cid, "node_id": comment_gid(repo, number, cid), "body": text}, {}

def rest_list_comments(owner, name, number):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    if number not in repo.issues:
        raise NotFound("Not Found")
    out = [{"id": c["id"], "node_id": comment_gid(repo, number, c["id"]), "body": c["body"]}
           for c in repo.comments.get(number, [])]
    return 200, out, {}

def rest_create_pull(owner, name, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    head = body.get("head")
    base = body.get("base")
    title = body.get("title")
    if not head or not base or not title:
        raise Unprocessable("head, base and title are required")
    head_sha = repo.refs.get("refs/heads/%s" % head)
    base_sha = repo.refs.get("refs/heads/%s" % base)
    if not head_sha or not base_sha:
        raise Unprocessable("head or base branch does not exist")
    repo.issue_seq += 1
    number = repo.issue_seq
    pr = {"number": number, "title": title, "body": body.get("body"),
          "state": "open", "merged": False, "merged_at": None,
          "merge_commit_sha": None, "head": head, "base": base,
          "head_sha": head_sha, "base_sha": base_sha}
    repo.pulls[number] = pr
    return 201, pull_json(repo, pr), {}

def rest_list_pulls(owner, name, query):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    state = query.get("state", ["open"])[0]
    pulls = list(repo.pulls.values())
    if state == "open":
        pulls = [p for p in pulls if p["state"] == "open"]
    elif state == "closed":
        pulls = [p for p in pulls if p["state"] == "closed"]
    pulls.sort(key=lambda p: p["number"])
    items, per_page, page, total = paginate([pull_json(repo, p) for p in pulls], query)
    return 200, items, {}

def rest_get_pull(owner, name, number):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    pr = repo.pulls.get(number)
    if pr is None:
        raise NotFound("Not Found")
    return 200, pull_json(repo, pr), {}

def rest_list_pull_files(owner, name, number):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    pr = repo.pulls.get(number)
    if pr is None:
        raise NotFound("Not Found")
    return 200, pr_files(repo, pr), {}

def rest_merge_pull(owner, name, number, body):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise NotFound("Not Found")
    pr = repo.pulls.get(number)
    if pr is None:
        raise NotFound("Not Found")
    if pr["merged"]:
        raise Conflict("Pull request is already merged")
    if pr["state"] != "open":
        raise Conflict("Pull request is not open")
    commit_oid, conflict = squash_merge(repo, pr)
    if conflict:
        raise MethodNotAllowed("Merge conflict: the pull request cannot be merged")
    pr["head_sha"] = repo.refs.get("refs/heads/%s" % pr["head"], pr["head_sha"])
    return 200, {"merged": True, "sha": commit_oid,
                 "message": "Merge pull request #%d: %s" % (number, pr["title"])}, {}

# ---------------------------------------------------------------- GraphQL

GQL_TYPE_FIELDS = {
    "Viewer": ["login"],
    "User": ["login", "id"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest"],
    "Ref": ["name"],
    "Issue": ["id", "number", "title", "state", "body"],
    "PullRequest": ["id", "number", "headRefName", "baseRefName", "mergeable",
                    "state", "merged", "files"],
    "IssueConnection": ["nodes", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "pageInfo", "totalCount"],
    "PullRequestFileConnection": ["nodes", "pageInfo"],
    "PageInfo": ["hasNextPage", "endCursor", "hasPreviousPage", "startCursor"],
    "PullRequestFile": ["path"],
    "IssueComment": ["id", "body"],
    "CommentEdge": ["node"],
    "CreateIssuePayload": ["issue"],
    "CloseIssuePayload": ["issue"],
    "CreatePullRequestPayload": ["pullRequest"],
    "MergePullRequestPayload": ["pullRequest"],
    "AddCommentPayload": ["commentEdge"],
    "SearchType": [],
}
GQL_SEARCH_ENUM = ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "COMMIT",
                   "CODE", "WIKI", "TOPIC", "PACKAGE", "MILESTONE", "PROJECT"]

class GQLParseError(Exception):
    pass

def _gql_tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "{}():[]!=$@&|":
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                                "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            tokens.append(("STRING", "".join(out)))
            i = j + 1
            continue
        if c == "." and text[i:i + 3] == "...":
            tokens.append("...")
            i += 3
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            try:
                tokens.append(("NUMBER", int(raw)))
            except ValueError:
                tokens.append(("NUMBER", float(raw)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("NAME", text[i:j]))
            i = j
            continue
        raise GQLParseError("unexpected character %r" % c)
    tokens.append(("EOF", None))
    return tokens

class GQLParser:
    def __init__(self, text):
        self.toks = _gql_tokenize(text)
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def expect(self, value):
        tok = self.next()
        if tok != value:
            raise GQLParseError("expected %r, got %r" % (value, tok))
        return tok

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "EOF":
            tok = self.peek()
            if tok == "{":
                sel = self.parse_selection_set()
                operations.append({"type": "query", "name": None, "variables": {},
                                   "var_defs": [], "selection": sel})
            elif tok == ("NAME", "fragment"):
                self.next()
                name = self.next()[1]
                self.expect("on")
                type_cond = self.next()[1]
                sel = self.parse_selection_set()
                fragments[name] = {"type": type_cond, "selection": sel}
            elif tok[0] == "NAME" and tok[1] in ("query", "mutation", "subscription"):
                op_type = self.next()[1]
                name = None
                if self.peek()[0] == "NAME":
                    name = self.next()[1]
                var_defs = []
                if self.peek() == "(":
                    var_defs = self.parse_var_defs()
                # directives (skip)
                while self.peek() == "@":
                    self.next()
                    self.next()
                    if self.peek() == "(":
                        self.parse_arguments()
                sel = self.parse_selection_set()
                operations.append({"type": op_type, "name": name,
                                   "variables": {}, "var_defs": var_defs,
                                   "selection": sel})
            else:
                raise GQLParseError("unexpected token %r" % (tok,))
        return operations, fragments

    def parse_var_defs(self):
        self.expect("(")
        defs = []
        while self.peek() != ")":
            tok = self.next()
            if tok != "$":
                raise GQLParseError("expected $ in var defs")
            name = self.next()[1]
            self.expect(":")
            type_name = self.parse_type()
            default = None
            if self.peek() == "=":
                self.next()
                default = self.parse_value()
            defs.append((name, type_name, default))
        self.expect(")")
        return defs

    def parse_type(self):
        tok = self.next()
        if tok == "[":
            inner = self.parse_type()
            self.expect("]")
            t = "[%s]" % inner
        elif tok[0] == "NAME":
            t = tok[1]
        else:
            raise GQLParseError("bad type")
        if self.peek() == "!":
            self.next()
            t += "!"
        return t

    def parse_selection_set(self):
        self.expect("{")
        fields = []
        while self.peek() != "}":
            fields.append(self.parse_field())
        self.expect("}")
        return fields

    def parse_field(self):
        tok = self.peek()
        if tok == "...":
            self.next()
            if self.peek()[0] == "NAME" and self.peek()[1] == "on":
                self.next()
                type_cond = self.next()[1]
                sel = self.parse_selection_set()
                return {"kind": "inline-fragment", "type": type_cond, "selection": sel}
            frag_name = self.next()[1]
            return {"kind": "fragment", "name": frag_name}
        # alias or name
        name_tok = self.next()
        if name_tok[0] != "NAME":
            raise GQLParseError("expected field name")
        name = name_tok[1]
        alias = None
        if self.peek() == ":":
            self.next()
            alias = name
            name = self.next()[1]
        args = {}
        if self.peek() == "(":
            args = self.parse_arguments()
        # directives (skip)
        while self.peek() == "@":
            self.next()
            self.next()
            if self.peek() == "(":
                self.parse_arguments()
        sel = None
        if self.peek() == "{":
            sel = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "selection": sel}

    def parse_arguments(self):
        self.expect("(")
        args = {}
        while self.peek() != ")":
            name = self.next()[1]
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        tok = self.next()
        if tok == "$":
            return ("$var", self.next()[1])
        if tok == "[":
            vals = []
            while self.peek() != "]":
                vals.append(self.parse_value())
            self.expect("]")
            return vals
        if tok == "{":
            obj = {}
            while self.peek() != "}":
                key = self.next()[1]
                self.expect(":")
                obj[key] = self.parse_value()
            self.expect("}")
            return obj
        if tok[0] == "STRING":
            return tok[1]
        if tok[0] == "NUMBER":
            return tok[1]
        if tok[0] == "NAME":
            if tok[1] == "true":
                return True
            if tok[1] == "false":
                return False
            if tok[1] == "null":
                return None
            return ("$enum", tok[1])
        raise GQLParseError("unexpected value token %r" % (tok,))

def gql_resolve_value(value, variables):
    if isinstance(value, tuple) and value and value[0] == "$var":
        return variables.get(value[1])
    if isinstance(value, tuple) and value and value[0] == "$enum":
        return value[1]
    if isinstance(value, list):
        return [gql_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: gql_resolve_value(v, variables) for k, v in value.items()}
    return value

def gql_resolve_args(args, variables):
    return {k: gql_resolve_value(v, variables) for k, v in args.items()}

class GQLExecutor:
    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append({"message": message})

    def execute(self, query_text, variables):
        variables = variables or {}
        try:
            operations, fragments = GQLParser(query_text).parse_document()
        except GQLParseError as exc:
            return {"data": None, "errors": [{"message": str(exc)}]}
        if not operations:
            return {"data": None, "errors": [{"message": "empty query"}]}
        op = operations[0]
        self.fragments = fragments
        data = {}
        for field in op["selection"]:
            self._resolve_root(field, data, variables, op["type"])
        if self.errors:
            return {"data": None, "errors": self.errors}
        return {"data": data}

    # -- root fields ---------------------------------------------------
    def _resolve_root(self, field, out, variables, op_type):
        if field["kind"] in ("inline-fragment", "fragment"):
            for sub in self._expand(field):
                self._resolve_root(sub, out, variables, op_type)
            return
        name = field["name"]
        args = gql_resolve_args(field.get("args", {}), variables)
        sel = field.get("selection") or []
        if op_type == "mutation":
            if name == "createIssue":
                out[field["alias"]] = self._mut_create_issue(args, sel, variables)
            elif name == "closeIssue":
                out[field["alias"]] = self._mut_close_issue(args, sel, variables)
            elif name == "createPullRequest":
                out[field["alias"]] = self._mut_create_pr(args, sel, variables)
            elif name == "mergePullRequest":
                out[field["alias"]] = self._mut_merge_pr(args, sel, variables)
            elif name == "addComment":
                out[field["alias"]] = self._mut_add_comment(args, sel, variables)
            else:
                self.error('Cannot query field "%s" on type "Mutation".' % name)
                out[field["alias"]] = None
            return
        if name == "viewer":
            out[field["alias"]] = self._resolve_typed(
                {"__type": "Viewer", "login": STORE.owner_login or ""}, sel, variables)
        elif name == "repository":
            repo = None
            owner = args.get("owner")
            rname = args.get("name")
            if owner is not None and rname is not None:
                repo = STORE.get_repo(owner, rname)
            out[field["alias"]] = (self._resolve_typed(
                self._repo_obj(repo), sel, variables) if repo else None)
        elif name == "node":
            out[field["alias"]] = self._resolve_node(args.get("id"), sel, variables)
        elif name == "__type":
            out[field["alias"]] = self._resolve_type(args.get("name"), sel, variables)
        elif name == "__schema":
            out[field["alias"]] = {}
        else:
            self.error('Cannot query field "%s" on type "Query".' % name)
            out[field["alias"]] = None

    def _expand(self, field):
        if field["kind"] == "inline-fragment":
            return field["selection"]
        frag = self.fragments.get(field["name"])
        return frag["selection"] if frag else []

    # -- typed object resolution ---------------------------------------
    def _resolve_typed(self, obj, selection, variables):
        if obj is None:
            return None
        type_name = obj.get("__type", "Object")
        out = {}
        for field in selection:
            if field["kind"] in ("inline-fragment", "fragment"):
                cond = field.get("type")
                if field["kind"] == "fragment":
                    frag = self.fragments.get(field["name"])
                    if not frag:
                        continue
                    cond = frag["type"]
                    sub = frag["selection"]
                else:
                    sub = field["selection"]
                if cond is None or cond == type_name:
                    sub_out = self._resolve_typed(obj, sub, variables)
                    if sub_out:
                        out.update(sub_out)
                continue
            name = field["name"]
            alias = field["alias"]
            allowed = GQL_TYPE_FIELDS.get(type_name, [])
            if name not in allowed:
                self.error('Cannot query field "%s" on type "%s".' % (name, type_name))
                out[alias] = None
                continue
            args = gql_resolve_args(field.get("args", {}), variables)
            sel = field.get("selection") or []
            out[alias] = self._field_value(obj, type_name, name, args, sel, variables)
        return out

    def _field_value(self, obj, type_name, name, args, sel, variables):
        if type_name == "Viewer" or type_name == "User":
            return obj.get("login")
        if type_name == "Repository":
            return self._repo_field(obj, name, args, sel, variables)
        if type_name == "Issue":
            return self._issue_field(obj, name, args, sel, variables)
        if type_name == "PullRequest":
            return self._pr_field(obj, name, args, sel, variables)
        if type_name in ("IssueConnection", "PullRequestConnection",
                         "PullRequestFileConnection"):
            if name == "nodes":
                nodes = obj.get("nodes", [])
                item_type = {"IssueConnection": "Issue",
                             "PullRequestConnection": "PullRequest",
                             "PullRequestFileConnection": "PullRequestFile"}[type_name]
                return [self._resolve_typed(n, sel, variables) for n in nodes]
            if name == "pageInfo":
                return self._resolve_typed(
                    {"__type": "PageInfo", **obj.get("pageInfo", {})}, sel, variables)
            if name == "totalCount":
                return len(obj.get("nodes", []))
        if type_name == "PageInfo":
            return obj.get(name)
        if type_name == "PullRequestFile":
            return obj.get("path")
        if type_name == "IssueComment":
            return obj.get(name)
        if type_name == "CommentEdge":
            if name == "node":
                node = obj.get("node")
                return self._resolve_typed(node, sel, variables) if node else None
        if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
            if name == "issue":
                issue = obj.get("issue")
                return self._resolve_typed(issue, sel, variables) if issue else None
        if type_name in ("CreatePullRequestPayload", "MergePullRequestPayload"):
            if name == "pullRequest":
                pr = obj.get("pullRequest")
                return self._resolve_typed(pr, sel, variables) if pr else None
        if type_name == "AddCommentPayload":
            if name == "commentEdge":
                edge = obj.get("commentEdge")
                return self._resolve_typed(edge, sel, variables) if edge else None
        if type_name == "__Type":
            if name == "fields":
                return [{"name": f} for f in obj.get("fields", [])]
            if name == "enumValues":
                return [{"name": e} for e in obj.get("enumValues", [])]
            return obj.get(name)
        return obj.get(name)

    # -- repository ----------------------------------------------------
    def _repo_obj(self, repo):
        owner = repo.owner or STORE.owner_login or ""
        return {"__type": "Repository", "_repo": repo, "id": repo_gid(repo),
                "databaseId": repo.database_id, "name": repo.name,
                "nameWithOwner": "%s/%s" % (owner, repo.name),
                "owner": {"__type": "User", "login": owner},
                "viewerPermission": "ADMIN", "hasIssuesEnabled": True}

    def _repo_field(self, obj, name, args, sel, variables):
        repo = obj["_repo"]
        if name == "id":
            return repo_gid(repo)
        if name == "databaseId":
            return repo.database_id
        if name == "name":
            return repo.name
        if name == "nameWithOwner":
            owner = repo.owner or STORE.owner_login or ""
            return "%s/%s" % (owner, repo.name)
        if name == "owner":
            return self._resolve_typed(
                {"__type": "User", "login": repo.owner or STORE.owner_login or ""},
                sel, variables)
        if name == "defaultBranchRef":
            tip = repo_tip(repo, None)
            if not tip:
                return None
            return self._resolve_typed(
                {"__type": "Ref", "name": repo.default_branch}, sel, variables)
        if name == "viewerPermission":
            return "ADMIN"
        if name == "hasIssuesEnabled":
            return True
        if name == "issue":
            number = args.get("number")
            issue = repo.issues.get(number) if isinstance(number, int) else None
            return self._resolve_typed(
                self._issue_obj(repo, issue), sel, variables) if issue else None
        if name == "issues":
            return self._resolve_typed(
                self._issue_connection(repo, args, sel, variables), sel, variables)
        if name == "pullRequests":
            return self._resolve_typed(
                self._pr_connection(repo, args, sel, variables), sel, variables)
        if name == "pullRequest":
            number = args.get("number")
            pr = repo.pulls.get(number) if isinstance(number, int) else None
            return self._resolve_typed(
                self._pr_obj(repo, pr), sel, variables) if pr else None
        return None

    def _issue_obj(self, repo, issue):
        return {"__type": "Issue", "_repo": repo, "_issue": issue,
                "id": issue_gid(repo, issue["number"]), "number": issue["number"],
                "title": issue["title"],
                "state": "OPEN" if issue["state"] == "open" else "CLOSED",
                "body": issue.get("body")}

    def _issue_field(self, obj, name, args, sel, variables):
        return obj.get(name)

    def _issue_connection(self, repo, args, sel, variables):
        issues = list(repo.issues.values())
        states = args.get("states")
        if states:
            wanted = set()
            for s in states:
                wanted.add("open" if str(s).upper() == "OPEN" else "closed")
            issues = [i for i in issues if i["state"] in wanted]
        order_by = args.get("orderBy") or {}
        direction = str(order_by.get("direction", "ASC")).upper()
        issues.sort(key=lambda i: i["number"], reverse=(direction == "DESC"))
        first = args.get("first")
        after = args.get("after")
        start = 0
        if after:
            try:
                padded = after + "=" * (-len(after) % 4)
                decoded = base64.urlsafe_b64decode(padded.encode()).decode()
                num = int(decoded.split(":")[-1])
                for idx, iss in enumerate(issues):
                    if iss["number"] == num:
                        start = idx + 1
                        break
            except Exception:
                start = 0
        page = issues[start:]
        if isinstance(first, int):
            page = page[:first]
        has_next = (start + len(page)) < len(issues)
        end_cursor = ""
        if page:
            end_cursor = b64url(("issue:%d" % page[-1]["number"]).encode())
        nodes = [self._issue_obj(repo, i) for i in page]
        return {"__type": "IssueConnection", "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}}

    def _pr_obj(self, repo, pr):
        head_sha = repo.refs.get("refs/heads/%s" % pr["head"], pr["head_sha"])
        base_sha = repo.refs.get("refs/heads/%s" % pr["base"], pr["base_sha"])
        state = "OPEN" if pr["state"] == "open" else ("MERGED" if pr["merged"] else "CLOSED")
        return {"__type": "PullRequest", "_repo": repo, "_pr": pr,
                "id": pr_gid(repo, pr["number"]), "number": pr["number"],
                "headRefName": pr["head"], "baseRefName": pr["base"],
                "mergeable": pr_mergeable(repo, pr), "state": state,
                "merged": pr["merged"], "head_sha": head_sha, "base_sha": base_sha}

    def _pr_field(self, obj, name, args, sel, variables):
        if name == "files":
            repo = obj["_repo"]
            pr = obj["_pr"]
            first = args.get("first")
            files = pr_files(repo, pr)
            nodes = [{"__type": "PullRequestFile", "path": f["filename"]}
                     for f in files]
            if isinstance(first, int):
                nodes = nodes[:first]
            return {"__type": "PullRequestFileConnection", "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": ""}}
        return obj.get(name)

    def _pr_connection(self, repo, args, sel, variables):
        pulls = sorted(repo.pulls.values(), key=lambda p: p["number"])
        first = args.get("first")
        if isinstance(first, int):
            pulls = pulls[:first]
        nodes = [self._pr_obj(repo, p) for p in pulls]
        return {"__type": "PullRequestConnection", "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": ""}}

    # -- node / introspection ------------------------------------------
    def _resolve_node(self, gid, sel, variables):
        if not gid:
            return None
        kind, rest = decode_global_id(gid)
        if kind == "Repository":
            parts = rest.split(":")
            if len(parts) >= 2:
                owner, name = parts[0], parts[1]
                repo = STORE.get_repo(owner, name)
                if repo:
                    return self._resolve_typed(self._repo_obj(repo), sel, variables)
            return None
        if kind == "Issue":
            parts = rest.split(":")
            if len(parts) >= 3:
                repo = STORE.get_repo(parts[0], parts[1])
                try:
                    num = int(parts[2])
                except ValueError:
                    return None
                if repo and num in repo.issues:
                    return self._resolve_typed(
                        self._issue_obj(repo, repo.issues[num]), sel, variables)
            return None
        if kind == "PullRequest":
            parts = rest.split(":")
            if len(parts) >= 3:
                repo = STORE.get_repo(parts[0], parts[1])
                try:
                    num = int(parts[2])
                except ValueError:
                    return None
                if repo and num in repo.pulls:
                    return self._resolve_typed(
                        self._pr_obj(repo, repo.pulls[num]), sel, variables)
            return None
        return None

    def _resolve_type(self, name, sel, variables):
        fields = GQL_TYPE_FIELDS.get(name, [])
        enum_values = GQL_SEARCH_ENUM if name == "SearchType" else []
        obj = {"__type": "__Type", "fields": fields, "enumValues": enum_values,
               "name": name}
        out = {}
        for field in sel:
            if field["kind"] in ("inline-fragment", "fragment"):
                continue
            fname = field["name"]
            if fname == "fields":
                out[field["alias"]] = [{"name": f} for f in fields]
            elif fname == "enumValues":
                out[field["alias"]] = [{"name": e} for e in enum_values]
            elif fname == "name":
                out[field["alias"]] = name
            else:
                out[field["alias"]] = None
        return out

    # -- mutations ------------------------------------------------------
    def _find_repo_by_gid(self, gid):
        kind, rest = decode_global_id(gid) if gid else (None, None)
        if kind != "Repository":
            return None
        parts = rest.split(":")
        if len(parts) < 2:
            return None
        return STORE.get_repo(parts[0], parts[1])

    def _find_issue_by_gid(self, gid):
        kind, rest = decode_global_id(gid) if gid else (None, None)
        if kind != "Issue":
            return None, None
        parts = rest.split(":")
        if len(parts) < 3:
            return None, None
        repo = STORE.get_repo(parts[0], parts[1])
        if not repo:
            return None, None
        try:
            num = int(parts[2])
        except ValueError:
            return None, None
        return repo, repo.issues.get(num)

    def _find_pr_by_gid(self, gid):
        kind, rest = decode_global_id(gid) if gid else (None, None)
        if kind != "PullRequest":
            return None, None
        parts = rest.split(":")
        if len(parts) < 3:
            return None, None
        repo = STORE.get_repo(parts[0], parts[1])
        if not repo:
            return None, None
        try:
            num = int(parts[2])
        except ValueError:
            return None, None
        return repo, repo.pulls.get(num)

    def _mut_create_issue(self, args, sel, variables):
        inp = args.get("input") or {}
        repo = self._find_repo_by_gid(inp.get("repositoryId"))
        if repo is None:
            self.error("repository not found")
            return None
        title = inp.get("title")
        if not title:
            self.error("title is required")
            return None
        repo.issue_seq += 1
        number = repo.issue_seq
        issue = {"number": number, "title": title, "body": inp.get("body"),
                 "state": "open", "labels": []}
        repo.issues[number] = issue
        payload = {"__type": "CreateIssuePayload",
                   "issue": self._issue_obj(repo, issue)}
        return self._resolve_typed(payload, sel, variables)

    def _mut_close_issue(self, args, sel, variables):
        inp = args.get("input") or {}
        repo, issue = self._find_issue_by_gid(inp.get("issueId"))
        if issue is None:
            self.error("issue not found")
            return None
        issue["state"] = "closed"
        payload = {"__type": "CloseIssuePayload",
                   "issue": self._issue_obj(repo, issue)}
        return self._resolve_typed(payload, sel, variables)

    def _mut_create_pr(self, args, sel, variables):
        inp = args.get("input") or {}
        repo = self._find_repo_by_gid(inp.get("repositoryId"))
        if repo is None:
            self.error("repository not found")
            return None
        head = inp.get("headRefName")
        base = inp.get("baseRefName")
        title = inp.get("title")
        head_sha = repo.refs.get("refs/heads/%s" % head) if head else None
        base_sha = repo.refs.get("refs/heads/%s" % base) if base else None
        if not head_sha or not base_sha or not title:
            self.error("head, base and title are required")
            return None
        repo.issue_seq += 1
        number = repo.issue_seq
        pr = {"number": number, "title": title, "body": inp.get("body"),
              "state": "open", "merged": False, "merged_at": None,
              "merge_commit_sha": None, "head": head, "base": base,
              "head_sha": head_sha, "base_sha": base_sha}
        repo.pulls[number] = pr
        payload = {"__type": "CreatePullRequestPayload",
                   "pullRequest": self._pr_obj(repo, pr)}
        return self._resolve_typed(payload, sel, variables)

    def _mut_merge_pr(self, args, sel, variables):
        inp = args.get("input") or {}
        repo, pr = self._find_pr_by_gid(inp.get("pullRequestId"))
        if pr is None:
            self.error("pull request not found")
            return None
        if pr["merged"]:
            self.error("pull request already merged")
            return None
        commit_oid, conflict = squash_merge(repo, pr)
        if conflict:
            self.error("merge conflict")
            return None
        payload = {"__type": "MergePullRequestPayload",
                   "pullRequest": self._pr_obj(repo, pr)}
        return self._resolve_typed(payload, sel, variables)

    def _mut_add_comment(self, args, sel, variables):
        inp = args.get("input") or {}
        repo, issue = self._find_issue_by_gid(inp.get("subjectId"))
        if issue is None:
            self.error("subject not found")
            return None
        body = inp.get("body")
        if body is None:
            self.error("body is required")
            return None
        repo.comment_seq += 1
        cid = repo.comment_seq
        comment = {"__type": "IssueComment",
                   "id": comment_gid(repo, issue["number"], cid), "body": body}
        repo.comments.setdefault(issue["number"], []).append(
            {"id": cid, "body": body})
        payload = {"__type": "AddCommentPayload",
                   "commentEdge": {"__type": "CommentEdge", "node": comment}}
        return self._resolve_typed(payload, sel, variables)

def graphql_execute(body):
    """body: parsed JSON (dict or list). Returns the response object."""
    if isinstance(body, list):
        return [graphql_execute_one(item) for item in body]
    return graphql_execute_one(body)

def graphql_execute_one(item):
    if not isinstance(item, dict):
        return {"data": None, "errors": [{"message": "request must be an object"}]}
    query = item.get("query")
    variables = item.get("variables") or {}
    if not isinstance(query, str) or not query.strip():
        return {"data": None, "errors": [{"message": "query is required"}]}
    return GQLExecutor().execute(query, variables)

# ---------------------------------------------------------------- MCP

MCP_REVISIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MCP_SERVER_INFO = {"name": FORGE_NAME, "version": FORGE_VERSION}

def mcp_tool(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required}}

MCP_TOOLS = [
    mcp_tool("create_repo", "Create a repository.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "name": {"type": "string", "description": "Repository name"},
              "description": {"type": "string", "description": "Repository description"},
              "private": {"type": "boolean", "description": "Whether the repository is private"}},
             ["name"]),
    mcp_tool("get_repo", "Get a repository.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "repo": {"type": "string", "description": "Repository name"}},
             ["owner", "repo"]),
    mcp_tool("create_issue", "Create an issue.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "repo": {"type": "string", "description": "Repository name"},
              "title": {"type": "string", "description": "Issue title"},
              "body": {"type": "string", "description": "Issue body"}},
             ["owner", "repo", "title"]),
    mcp_tool("list_issues", "List issues in a repository.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "repo": {"type": "string", "description": "Repository name"},
              "state": {"type": "string", "description": "Issue state filter (open, closed, all)"}},
             ["owner", "repo"]),
    mcp_tool("create_pull_request", "Create a pull request.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "repo": {"type": "string", "description": "Repository name"},
              "title": {"type": "string", "description": "Pull request title"},
              "body": {"type": "string", "description": "Pull request body"},
              "head": {"type": "string", "description": "Head branch name"},
              "base": {"type": "string", "description": "Base branch name"}},
             ["owner", "repo", "title", "head", "base"]),
    mcp_tool("merge_pull_request", "Merge a pull request.",
             {"owner": {"type": "string", "description": "Repository owner login"},
              "repo": {"type": "string", "description": "Repository name"},
              "pull_number": {"type": "integer", "description": "Pull request number"}},
             ["owner", "repo", "pull_number"]),
]

def mcp_tool_result(obj, is_error=False):
    text = json.dumps(obj, separators=(",", ":")) if not isinstance(obj, str) else obj
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": obj if not isinstance(obj, str) else {"message": obj},
            "isError": is_error}

def mcp_call_tool(name, args):
    """Execute a tool. Returns (result_obj, is_error)."""
    def need(*keys):
        missing = [k for k in keys if k not in args or args.get(k) in (None, "")]
        if missing:
            return "missing required argument: %s" % ", ".join(missing)
        return None
    if name == "create_repo":
        err = need("name")
        if err:
            return err, True
        rname = args["name"]
        if rname in STORE.repos:
            return "Repository %s already exists" % rname, True
        owner = args.get("owner")
        if owner:
            STORE.bind_owner(owner)
        repo = STORE.create_repo(rname, bool(args.get("private", False)),
                                 args.get("description"))
        return repo_json(repo), False
    if name == "get_repo":
        err = need("owner", "repo")
        if err:
            return err, True
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return "Repository %s/%s not found" % (args["owner"], args["repo"]), True
        return repo_json(repo), False
    if name == "create_issue":
        err = need("owner", "repo", "title")
        if err:
            return err, True
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return "Repository %s/%s not found" % (args["owner"], args["repo"]), True
        repo.issue_seq += 1
        number = repo.issue_seq
        issue = {"number": number, "title": args["title"],
                 "body": args.get("body"), "state": "open", "labels": []}
        repo.issues[number] = issue
        return issue_json(repo, issue), False
    if name == "list_issues":
        err = need("owner", "repo")
        if err:
            return err, True
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return "Repository %s/%s not found" % (args["owner"], args["repo"]), True
        state = args.get("state") or "open"
        issues = list(repo.issues.values())
        if state == "open":
            issues = [i for i in issues if i["state"] == "open"]
        elif state == "closed":
            issues = [i for i in issues if i["state"] == "closed"]
        issues.sort(key=lambda i: i["number"])
        return [issue_json(repo, i) for i in issues], False
    if name == "create_pull_request":
        err = need("owner", "repo", "title", "head", "base")
        if err:
            return err, True
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return "Repository %s/%s not found" % (args["owner"], args["repo"]), True
        head = args["head"]
        base = args["base"]
        head_sha = repo.refs.get("refs/heads/%s" % head)
        base_sha = repo.refs.get("refs/heads/%s" % base)
        if not head_sha or not base_sha:
            return "head or base branch does not exist", True
        repo.issue_seq += 1
        number = repo.issue_seq
        pr = {"number": number, "title": args["title"], "body": args.get("body"),
              "state": "open", "merged": False, "merged_at": None,
              "merge_commit_sha": None, "head": head, "base": base,
              "head_sha": head_sha, "base_sha": base_sha}
        repo.pulls[number] = pr
        return pull_json(repo, pr), False
    if name == "merge_pull_request":
        err = need("owner", "repo", "pull_number")
        if err:
            return err, True
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return "Repository %s/%s not found" % (args["owner"], args["repo"]), True
        try:
            number = int(args["pull_number"])
        except (TypeError, ValueError):
            return "pull_number must be an integer", True
        pr = repo.pulls.get(number)
        if pr is None:
            return "Pull request #%d not found" % number, True
        if pr["merged"]:
            return "Pull request is already merged", True
        commit_oid, conflict = squash_merge(repo, pr)
        if conflict:
            return "Merge conflict: the pull request cannot be merged", True
        return pull_json(repo, pr), False
    return "Unknown tool: %s" % name, True

def mcp_handle(body_bytes):
    """Handle one POST /mcp body. Returns (http_status, response_bytes)."""
    try:
        req = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        err = {"jsonrpc": "2.0", "id": None,
               "error": {"code": -32700, "message": "Parse error"}}
        return 400, json.dumps(err).encode()
    if not isinstance(req, dict):
        err = {"jsonrpc": "2.0", "id": None,
               "error": {"code": -32600, "message": "Invalid Request"}}
        return 400, json.dumps(err).encode()
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    # notifications (no id): acknowledge with 202 and no body
    if req_id is None:
        return 202, b""
    def result(res):
        return 200, json.dumps({"jsonrpc": "2.0", "id": req_id, "result": res}).encode()
    def rpc_error(code, message):
        return 200, json.dumps({"jsonrpc": "2.0", "id": req_id,
                                "error": {"code": code, "message": message}}).encode()
    if method == "server/discover":
        return result({
            "protocolVersion": "2026-07-28",
            "serverInfo": dict(MCP_SERVER_INFO),
            "capabilities": {"tools": {}},
            "supportedVersions": list(MCP_REVISIONS),
            "instructions": "A GitHub-compatible git forge: repositories, issues, pull requests.",
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(MCP_SERVER_INFO)},
        })
    if method == "initialize":
        offered = params.get("protocolVersion")
        negotiated = offered if offered in MCP_REVISIONS else "2025-11-25"
        return result({
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": dict(MCP_SERVER_INFO),
        })
    if method == "ping":
        return result({})
    if method == "tools/list":
        # return a deep copy so the list is byte-identical every call
        return result({"tools": json.loads(json.dumps(MCP_TOOLS))})
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {}
        known = {t["name"] for t in MCP_TOOLS}
        if tname not in known:
            return rpc_error(-32601, "Unknown tool: %s" % tname)
        if not isinstance(targs, dict):
            return rpc_error(-32602, "arguments must be an object")
        obj, is_error = mcp_call_tool(tname, targs)
        return result(mcp_tool_result(obj, is_error))
    return rpc_error(-32601, "Method not found: %s" % method)

# ---------------------------------------------------------------- HTTP server

SHOP_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "")

def authorized(headers):
    auth = headers.get("Authorization", "")
    if SHOP_TOKEN:
        return auth == "Bearer " + SHOP_TOKEN
    return auth.startswith("Bearer ") and len(auth) > len("Bearer ")

def json_bytes(obj):
    return json.dumps(obj, separators=(",", ":")).encode()

class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "makefun-forge/1.0"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):
        pass

    # -- plumbing ------------------------------------------------------
    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def _send(self, status, body, content_type, extra=None):
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if extra:
                for k, v in extra.items():
                    if v is not None:
                        self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status, obj, extra=None):
        headers = {"X-GitHub-Request-Id": STORE.next_req_id()}
        if extra:
            headers.update(extra)
        self._send(status, json_bytes(obj) if obj is not None else b"",
                   "application/json", headers)

    # -- verbs ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- dispatch ------------------------------------------------------
    def _dispatch(self, method):
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
        except Exception:
            body = b""
        try:
            with STORE.lock:
                status, payload, ctype, extra = self._route(method, body)
        except BadRequest as exc:
            status, payload, ctype, extra = 400, json_bytes({"message": str(exc) or "Bad Request"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        except NotFound as exc:
            status, payload, ctype, extra = 404, json_bytes({"message": str(exc) or "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        except Unprocessable as exc:
            status, payload, ctype, extra = 422, json_bytes({"message": exc.message}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        except Conflict as exc:
            status, payload, ctype, extra = 409, json_bytes({"message": exc.message}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        except MethodNotAllowed as exc:
            status, payload, ctype, extra = 405, json_bytes({"message": exc.message}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            status, payload, ctype, extra = 500, json_bytes({"message": "Internal Server Error: %s" % exc}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        self._send(status, payload, ctype, extra)

    def _route(self, method, body):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        host = self.headers.get("Host", "127.0.0.1")
        # -- healthz (no auth) -----------------------------------------
        if path == "/healthz":
            if method != "GET":
                return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            return 200, json_bytes({"ok": True}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        # -- git smart HTTP (no auth) ----------------------------------
        m = re.match(r"^/([^/]+)/([^/]+)\.git/(info/refs|git-upload-pack|git-receive-pack)$", path)
        if m:
            owner = urllib.parse.unquote(m.group(1))
            repo_name = urllib.parse.unquote(m.group(2))
            action = m.group(3)
            repo = STORE.get_repo(owner, repo_name)
            if repo is None:
                return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            if action == "info/refs":
                service = query.get("service", [None])[0]
                proto2 = (self.headers.get("Git-Protocol", "") == "version=2")
                if service == "git-upload-pack":
                    data = advertisement(repo, "git-upload-pack", proto2)
                    return 200, data, "application/x-git-upload-pack-advertisement", {}
                if service == "git-receive-pack":
                    data = advertisement(repo, "git-receive-pack", False)
                    return 200, data, "application/x-git-receive-pack-advertisement", {}
                return 400, json_bytes({"message": "service is required"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            if action == "git-upload-pack":
                if method != "POST":
                    return 405, json_bytes({"message": "Method Not Allowed"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
                proto2 = (self.headers.get("Git-Protocol", "") == "version=2")
                data = handle_fetch_v2(repo, body) if proto2 else handle_fetch_v0(repo, body)
                return 200, data, "application/x-git-upload-pack-result", {}
            # git-receive-pack
            if method != "POST":
                return 405, json_bytes({"message": "Method Not Allowed"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            data = handle_receive_pack(repo, body)
            return 200, data, "application/x-git-receive-pack-result", {}
        # -- GraphQL ----------------------------------------------------
        if path == "/api/graphql":
            if method != "POST":
                return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            if not authorized(self.headers):
                return 401, json_bytes({"message": "Requires authentication"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                return 400, json_bytes({"message": "Malformed JSON body"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            result = graphql_execute(payload)
            return 200, json_bytes(result), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        # -- MCP --------------------------------------------------------
        if path == "/mcp":
            if method != "POST":
                return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            if not authorized(self.headers):
                return 401, json_bytes({"message": "Requires authentication"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            status, data = mcp_handle(body)
            return status, data, "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        # -- REST v3 ----------------------------------------------------
        if path == "/api/v3" or path == "/api/v3/":
            if not authorized(self.headers):
                return 401, json_bytes({"message": "Requires authentication"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            if method != "GET":
                return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
            return 200, json_bytes({"current_user_url": "/api/v3/user",
                                    "rate_limit_url": "/api/v3/rate_limit"}), "application/json", {
                                        "X-GitHub-Request-Id": STORE.next_req_id(),
                                        "X-OAuth-Scopes": "repo, read:org"}
        if path.startswith("/api/v3/"):
            return self._route_rest(method, path[len("/api/v3"):], query, body, host)
        return 404, json_bytes({"message": "Not Found"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}

    def _route_rest(self, method, sub, query, body, host):
        if not authorized(self.headers):
            return 401, json_bytes({"message": "Requires authentication"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        # parse a JSON body when present
        data = {}
        if body:
            try:
                parsed_body = json.loads(body.decode("utf-8"))
                data = parsed_body if isinstance(parsed_body, dict) else {}
            except Exception:
                return 400, json_bytes({"message": "Malformed JSON body"}), "application/json", {"X-GitHub-Request-Id": STORE.next_req_id()}
        def ok(status, obj, extra=None):
            headers = {"X-GitHub-Request-Id": STORE.next_req_id()}
            if extra:
                headers.update(extra)
            return status, (json_bytes(obj) if obj is not None else b""), "application/json", headers
        # -- /meta, /user ----------------------------------------------
        if sub == "/meta" and method == "GET":
            return ok(200, {"installed_version": FORGE_VERSION})
        if sub == "/user" and method == "GET":
            login = STORE.owner_login or ""
            return ok(200, {"login": login, "id": STORE.user_id, "type": "User",
                            "node_id": global_id("User", login)})
        if sub == "/user/repos":
            if method == "GET":
                status, items, extra = rest_list_repos(host, "/api/v3/user/repos", query)
                return ok(status, items, extra)
            if method == "POST":
                status, obj, _ = rest_create_repo(data)
                return ok(status, obj)
            return ok(404, {"message": "Not Found"})
        # -- /repos/... -------------------------------------------------
        if sub.startswith("/repos/"):
            rest = sub[len("/repos/"):]
            parts = [urllib.parse.unquote(p) for p in rest.split("/")]
            if len(parts) < 2:
                return ok(404, {"message": "Not Found"})
            owner, name = parts[0], parts[1]
            tail = parts[2:]
            if not tail:
                if method == "GET":
                    status, obj, _ = rest_get_repo(owner, name)
                    return ok(status, obj)
                if method == "PATCH":
                    status, obj, _ = rest_patch_repo(owner, name, data)
                    return ok(status, obj)
                if method == "DELETE":
                    status, obj, _ = rest_delete_repo(owner, name)
                    return ok(status, obj)
                return ok(404, {"message": "Not Found"})
            if tail[0] == "branches":
                if len(tail) == 1 and method == "GET":
                    status, obj, _ = rest_list_branches(owner, name)
                    return ok(status, obj)
                if len(tail) == 2 and method == "GET":
                    status, obj, _ = rest_get_branch(owner, name, tail[1])
                    return ok(status, obj)
                return ok(404, {"message": "Not Found"})
            if tail[0] == "git" and len(tail) >= 2 and tail[1] == "refs":
                if len(tail) == 2 and method == "POST":
                    status, obj, _ = rest_create_ref(owner, name, data)
                    return ok(status, obj)
                if len(tail) > 2 and method == "GET":
                    status, obj, _ = rest_get_ref(owner, name, "/".join(tail[2:]))
                    return ok(status, obj)
                return ok(404, {"message": "Not Found"})
            if tail[0] == "commits" and method == "GET":
                status, obj, _ = rest_list_commits(owner, name, query)
                return ok(status, obj)
            if tail[0] == "contents" and len(tail) >= 2:
                cpath = "/".join(tail[1:])
                if method == "PUT":
                    status, obj, _ = rest_put_contents(owner, name, cpath, data)
                    return ok(status, obj)
                if method == "GET":
                    status, obj, _ = rest_get_contents(owner, name, cpath, query)
                    return ok(status, obj)
                if method == "DELETE":
                    status, obj, _ = rest_delete_contents(owner, name, cpath, data)
                    return ok(status, obj)
                return ok(404, {"message": "Not Found"})
            if tail[0] == "labels" and method == "POST":
                status, obj, _ = rest_create_label(owner, name, data)
                return ok(status, obj)
            if tail[0] == "issues":
                if len(tail) == 1:
                    if method == "GET":
                        status, obj, _ = rest_list_issues(owner, name, query)
                        return ok(status, obj)
                    if method == "POST":
                        status, obj, _ = rest_create_issue(owner, name, data)
                        return ok(status, obj)
                    return ok(404, {"message": "Not Found"})
                try:
                    number = int(tail[1])
                except ValueError:
                    return ok(404, {"message": "Not Found"})
                if len(tail) == 2:
                    if method == "GET":
                        status, obj, _ = rest_get_issue(owner, name, number)
                        return ok(status, obj)
                    if method == "PATCH":
                        status, obj, _ = rest_patch_issue(owner, name, number, data)
                        return ok(status, obj)
                    return ok(404, {"message": "Not Found"})
                if len(tail) == 3 and tail[2] == "comments":
                    if method == "GET":
                        status, obj, _ = rest_list_comments(owner, name, number)
                        return ok(status, obj)
                    if method == "POST":
                        status, obj, _ = rest_create_comment(owner, name, number, data)
                        return ok(status, obj)
                    return ok(404, {"message": "Not Found"})
                return ok(404, {"message": "Not Found"})
            if tail[0] == "pulls":
                if len(tail) == 1:
                    if method == "GET":
                        status, obj, _ = rest_list_pulls(owner, name, query)
                        return ok(status, obj)
                    if method == "POST":
                        status, obj, _ = rest_create_pull(owner, name, data)
                        return ok(status, obj)
                    return ok(404, {"message": "Not Found"})
                try:
                    number = int(tail[1])
                except ValueError:
                    return ok(404, {"message": "Not Found"})
                if len(tail) == 2 and method == "GET":
                    status, obj, _ = rest_get_pull(owner, name, number)
                    return ok(status, obj)
                if len(tail) == 3 and tail[2] == "files" and method == "GET":
                    status, obj, _ = rest_list_pull_files(owner, name, number)
                    return ok(status, obj)
                if len(tail) == 3 and tail[2] == "merge" and method == "PUT":
                    status, obj, _ = rest_merge_pull(owner, name, number, data)
                    return ok(status, obj)
                return ok(404, {"message": "Not Found"})
            return ok(404, {"message": "Not Found"})
        return ok(404, {"message": "Not Found"})

# ---------------------------------------------------------------- lifecycle

def http_json(url, payload, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()

def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))

def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ForgeHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    if not ENV_URL:
        # Standalone mode (local dev/testing): fixed port, serve forever.
        fixed = int(os.environ.get("FORGE_PORT", "8080"))
        server.server_close()
        server = ThreadingHTTPServer(("127.0.0.1", fixed), ForgeHandler)
        server.daemon_threads = True
        sys.stderr.write("makefun-forge standalone on http://127.0.0.1:%d\n" % fixed)
        sys.stderr.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    # Trial mode: register, then serve while polling /done.
    env_base = ENV_URL.rstrip("/")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sys.stderr.write("makefun-forge listening on 127.0.0.1:%d; registering with %s\n" % (port, env_base))
    sys.stderr.flush()
    deadline = time.time() + 120
    delay = 0.2
    registered = False
    while time.time() < deadline and not registered:
        try:
            status, _ = http_json(env_base + "/register", {"port": port}, timeout=5)
            if status in (200, 201, 409):
                registered = True
        except Exception as exc:
            sys.stderr.write("register retry: %s\n" % exc)
            sys.stderr.flush()
            time.sleep(delay)
            delay = min(delay * 2, 5)
    if not registered:
        sys.stderr.write("failed to register with the env\n")
        server.shutdown()
        return 1
    delay = 0.25
    while True:
        try:
            _status, payload = http_get_json(env_base + "/done", timeout=5)
            if isinstance(payload, dict) and payload.get("done") is True:
                break
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    server.shutdown()
    server.server_close()
    return 0

if __name__ == "__main__":
    def _term(signum, frame):
        sys.exit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    sys.exit(main())
