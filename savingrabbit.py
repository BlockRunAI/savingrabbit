#!/usr/bin/env python3
"""SavingRabbit: what your AI coding spend shipped, and where to save.

Pulls spend from the vendors' own admin APIs, joins it to the pull requests
that merged, and reports cost per merged PR by tool, model and team. Then it
helps you act on it: see which model BlockRun's router would pick for a task,
and replay real tasks on cheaper models to check the quality holds.

Read-only. Standard library only. Reports by team and tool, never by person.
Apache License 2.0. Made by BlockRun (https://blockrun.ai).
"""
import argparse, base64, csv, datetime as dt, glob, html, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict

__version__ = "0.3.0"
DATA = os.environ.get("SAVINGRABBIT_DATA", "savingrabbit-data")
UA = f"savingrabbit/{__version__} (+https://github.com/BlockRunAI/savingrabbit)"

# ---------------------------------------------------------------- helpers ---

def http(url, headers=None, body=None, tries=4):
    """JSON request with retries on 429/5xx. Errors show status and body, never the request headers (they hold keys)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(int(e.headers.get("Retry-After") or 2 ** attempt))
                continue
            sys.exit(f"{e.code} from {url.split('?')[0]}: {e.read()[:300].decode('utf-8', 'replace')}")

def need(var, hint):
    return os.environ.get(var) or sys.exit(f"set {var} ({hint})")

def days(since, until):
    d, end = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=1)

def epoch(day, end=False):
    t = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc)
    return int((t + dt.timedelta(days=1 if end else 0)).timestamp())

def slug(name):
    """'Claude Opus 5.5' / 'claude-opus-5.5' / 'anthropic/claude-opus-5-5' -> 'claude-opus-5-5'."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower().split("/")[-1]).strip("-")

def write_jsonl(name, rows, replace_source=None):
    """Write rows; with replace_source, keep other sources' rows already in the file."""
    os.makedirs(DATA, exist_ok=True)
    keep = [r for r in read_jsonl(name) if r.get("source") != replace_source] if replace_source else []
    with open(os.path.join(DATA, name), "w") as f:
        for r in keep + rows:
            f.write(json.dumps(r) + "\n")
    print(f"{name}: {len(rows)} rows from {replace_source or 'this run'}", file=sys.stderr)

def read_jsonl(name):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]

def spend_row(day, source, tool, model, actor, cents):
    """The one shape every spend source is normalized into. `key` is what PR attribution joins on."""
    key = slug(model) if tool == "Claude Code" else slug(tool)
    return {"day": day, "source": source, "tool": tool, "model": slug(model) if model else "", "actor": actor or "",
            "cents": round(float(cents or 0), 4), "key": key}

# ------------------------------------------------------- spend: Anthropic ---
ANTHROPIC = "https://api.anthropic.com/v1/organizations"
ANTHROPIC_HEADERS = lambda: {"x-api-key": need("ANTHROPIC_ADMIN_KEY", "a Claude Console Admin API key"),
                             "anthropic-version": "2026-10-21"}

def flatten_claude_code(rec):
    """Claude Code Analytics API record -> one spend row per model (cost is in cents)."""
    actor = rec.get("actor") or {}
    who = actor.get("email_address") or actor.get("api_key_name")
    return [spend_row(rec["date"][:10], "claude-code", "Claude Code", m["model"], who,
                      (m.get("estimated_cost") or {}).get("amount")) for m in rec.get("model_breakdown") or []]

def pull_claude_code(a):
    rows, h = [], ANTHROPIC_HEADERS()
    for day in days(a.since, a.until):
        page = None
        while True:
            q = {"starting_at": day, "limit": 1000, **({"page": page} if page else {})}
            res = http(f"{ANTHROPIC}/usage_report/claude_code?{urllib.parse.urlencode(q)}", h)
            for rec in res.get("data", []):
                rows += flatten_claude_code(rec)
            if not res.get("has_more"):
                break
            page = res["next_page"]
    write_jsonl("spend.jsonl", rows, "claude-code")

def flatten_anthropic_cost(bucket):
    """Cost report bucket (grouped by workspace_id, description) -> spend rows. Amounts are cent strings."""
    return [spend_row(bucket["starting_at"][:10], "anthropic-api", f"Claude API · {r.get('workspace_id') or 'default workspace'}",
                      r.get("model") or "", None, r.get("amount")) for r in bucket.get("results", [])]

def pull_anthropic_api(a):
    rows, h, page = [], ANTHROPIC_HEADERS(), None
    while True:
        q = [("starting_at", f"{a.since}T00:00:00Z"), ("ending_at", f"{(dt.date.fromisoformat(a.until) + dt.timedelta(days=1))}T00:00:00Z"),
             ("group_by[]", "workspace_id"), ("group_by[]", "description"), ("limit", 31)] + ([("page", page)] if page else [])
        res = http(f"{ANTHROPIC}/cost_report?{urllib.parse.urlencode(q)}", h)
        for b in res.get("data", []):
            rows += flatten_anthropic_cost(b)
        if not res.get("has_more"):
            break
        page = res["next_page"]
    write_jsonl("spend.jsonl", rows, "anthropic-api")

# ---------------------------------------------------------- spend: OpenAI ---

def flatten_openai_cost(bucket, tool_for_project):
    day = dt.datetime.fromtimestamp(bucket["start_time"], dt.timezone.utc).date().isoformat()
    return [spend_row(day, "openai", tool_for_project.get(r.get("project_id"), f"OpenAI API · {r.get('project_id') or 'default project'}"),
                      r.get("line_item") or "", None, float((r.get("amount") or {}).get("value") or 0) * 100)
            for r in bucket.get("results", [])]

def pull_openai(a):
    h = {"Authorization": f"Bearer {need('OPENAI_ADMIN_KEY', 'an OpenAI organization Admin key')}"}
    tools = dict(p.split("=", 1) for p in (a.label or []))  # proj_abc=Codex
    rows, page = [], None
    while True:
        q = [("start_time", epoch(a.since)), ("end_time", epoch(a.until, end=True)), ("bucket_width", "1d"),
             ("group_by", "project_id"), ("group_by", "line_item"), ("limit", 180)] + ([("page", page)] if page else [])
        res = http(f"https://api.openai.com/v1/organization/costs?{urllib.parse.urlencode(q)}", h)
        for b in res.get("data", []):
            rows += flatten_openai_cost(b, tools)
        if not res.get("has_more"):
            break
        page = res["next_page"]
    write_jsonl("spend.jsonl", rows, "openai")

# ---------------------------------------------------------- spend: Cursor ---

def flatten_cursor(ev):
    day = dt.datetime.fromtimestamp(int(ev["timestamp"]) / 1000, dt.timezone.utc).date().isoformat()
    cents = ev.get("chargedCents")
    if cents is None:
        cents = (ev.get("tokenUsage") or {}).get("totalCents", 0)
    return spend_row(day, "cursor", "Cursor", ev.get("model") or "", ev.get("userEmail"), cents)

def pull_cursor(a):
    key = need("CURSOR_ADMIN_KEY", "a Cursor team Admin API key")
    h = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}
    rows, page = [], 1
    while True:
        res = http("https://api.cursor.com/teams/filtered-usage-events", h,
                   {"startDate": epoch(a.since) * 1000, "endDate": epoch(a.until, end=True) * 1000 - 1, "page": page, "pageSize": 1000})
        rows += [flatten_cursor(e) for e in res.get("usageEvents", [])]
        if not (res.get("pagination") or {}).get("hasNextPage"):
            break
        page += 1  # ponytail: the API allows 60 requests/minute per team; add a sleep if a big team hits it
    write_jsonl("spend.jsonl", rows, "cursor")

# --------------------------------------------------------- spend: Copilot ---

def pull_copilot(a):
    """Copilot is billed per seat, so spend = seats x price x months in the window."""
    h = {"Authorization": f"Bearer {need('GITHUB_TOKEN', 'a token that can read the org Copilot billing')}",
         "Accept": "application/vnd.github+json"}
    seats = (http(f"https://api.github.com/orgs/{a.org}/copilot/billing", h).get("seat_breakdown") or {}).get("total", 0)
    months = (dt.date.fromisoformat(a.until) - dt.date.fromisoformat(a.since)).days / 30.4 + 1 / 30.4
    write_jsonl("spend.jsonl", [spend_row(a.since, "copilot", "Copilot", "", None, seats * a.seat_price * 100 * months)], "copilot")

# ------------------------------------------------- spend: BlockRun gateway ---

def flatten_blockrun(report, day):
    return [spend_row(day, "blockrun", "BlockRun gateway", m["model"], None, (m.get("revenueUsd") or 0) * 100)
            for m in report.get("perModel", [])]

def pull_blockrun(a):
    """Spend through the BlockRun gateway for a wallet, from its public reconciliation report."""
    rows = []
    for day in days(a.since, a.until):
        rep = http(f"https://blockrun.ai/api/v1/wallet/{a.wallet}/reconciliation?{urllib.parse.urlencode({'date': day, 'limit': 1})}")
        rows += flatten_blockrun(rep, day)
    write_jsonl("spend.jsonl", rows, "blockrun")

# ------------------------------------------------- spend: your own proxy log ---
# Public user-agent prefixes of popular coding agents and SDKs, as they appear on an LLM gateway.
CLIENTS = [
    (r"^claude-cli/", "Claude Code"), (r"^codex[-_ ]?(tui|cli|desktop|exec)?", "Codex"), (r"^codex desktop/", "Codex"),
    (r"^cursor", "Cursor"), (r"^opencode/", "opencode"), (r"^cline", "Cline"), (r"^roo-?code", "Roo Code"),
    (r"^aider", "Aider"), (r"^continue", "Continue"), (r"^zed", "Zed"), (r"^windsurf", "Windsurf"),
    (r"^clawrouter/", "ClawRouter"), (r"^franklin/", "Franklin"), (r"^deepseek-harness/", "DeepSeek Harness"),
    (r"^agents/python", "OpenAI Agents SDK"), (r"^(async)?openai/python", "OpenAI SDK (Python)"),
    (r"^openai/js", "OpenAI SDK (JS)"), (r"^anthropic/(python|js)", "Anthropic SDK"),
    (r"^blockrun-(ts|python|go)/", "BlockRun SDK"), (r"ai-sdk/", "Vercel AI SDK"), (r"^langchain", "LangChain"),
]

def classify_client(ua):
    ua = (ua or "").strip().lower()
    for pattern, tool in CLIENTS:
        if re.search(pattern, ua):
            return tool
    return "Other" if ua else "Unknown"

def to_day(v):
    """ISO date/time, or epoch seconds or milliseconds -> YYYY-MM-DD (UTC)."""
    s = str(v or "").strip()
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        t = float(s)
        return dt.datetime.fromtimestamp(t / 1000 if t > 1e11 else t, dt.timezone.utc).date().isoformat()
    return s[:10]

def import_log(a):
    """Import a JSONL or CSV request log from your own LLM proxy (LiteLLM, nginx, a gateway) and tag each
    call by the client that sent it. Field names are configurable; cost is in USD."""
    with open(a.file) as f:
        recs = list(csv.DictReader(f)) if a.file.endswith(".csv") else [json.loads(l) for l in f if l.strip()]
    rows = [spend_row(to_day(r.get(a.time_field)), "proxy-log", classify_client(r.get(a.ua_field)), r.get(a.model_field) or "",
                      r.get(a.user_field), float(r.get(a.cost_field) or 0) * 100) for r in recs]
    write_jsonl("spend.jsonl", rows, "proxy-log")


# ------------------------------------------------ personal mode: no keys ---
# Reads only token counts from Claude Code's local session logs (never prompts or outputs), prices them at
# list price from BlockRun's public model catalog, and joins them to your own commits in local git repos.

def price_table():
    """slug -> USD per million tokens {input, output, cache_read, cache_write}, from BlockRun's public catalog."""
    out = {}
    res = http("https://blockrun.ai/api/v1/models")
    for m in (res.get("data", []) if isinstance(res, dict) else res):
        if m.get("id", "").startswith("anthropic/") and m.get("pricing"):
            out[slug(m["id"])] = m["pricing"]
    return out

def price_for(model, prices):
    """Longest catalog slug that the log's model id starts with ('claude-opus-5-5-20261101' -> 'claude-opus-5-5')."""
    s = slug(model)
    hits = [k for k in prices if s == k or s.startswith(k + "-")]
    return prices[max(hits, key=len)] if hits else None

def local_usage_rows(lines, prices, since, until):
    """Claude Code JSONL lines -> spend rows. Streaming writes the same message more than once, so
    rows are de-duplicated on (message id, request id)."""
    seen, rows, unpriced = set(), [], set()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") or {}
        usage, model = msg.get("usage"), msg.get("model")
        if e.get("type") != "assistant" or not usage or not model or model.startswith("<"):
            continue
        day = to_day(e.get("timestamp"))
        if not (since <= day <= until):
            continue
        key = (msg.get("id"), e.get("requestId"))
        if key != (None, None) and key in seen:
            continue
        seen.add(key)
        if e.get("costUSD") is not None:
            usd = float(e["costUSD"])
        elif (pr := price_for(model, prices)):
            usd = (usage.get("input_tokens", 0) * pr.get("input", 0) + usage.get("output_tokens", 0) * pr.get("output", 0)
                   + usage.get("cache_read_input_tokens", 0) * pr.get("cache_read", 0)
                   + usage.get("cache_creation_input_tokens", 0) * pr.get("cache_write", 0)) / 1e6
        else:
            unpriced.add(model)
            continue
        row = spend_row(day, "local", "Claude Code", model, "me", usd * 100)
        row["cwd"] = e.get("cwd") or ""
        rows.append(row)
    return rows, unpriced

def attribute_by_session(commits, spend, repo):
    """For commits with no agent trailer: if Claude Code ran in this repo that day, credit the model
    you spent most on there that day. Many teams strip co-author trailers, so trailers alone undercount."""
    root = os.path.realpath(repo)
    by_day = defaultdict(lambda: defaultdict(float))
    for r in spend:
        cwd = os.path.realpath(r.get("cwd") or "/nonexistent")
        if cwd == root or cwd.startswith(root + os.sep):
            by_day[r["day"]][r["key"]] += r["cents"]
    for c in commits:
        day = c["merged_at"][:10]
        if not c["agents"] and by_day.get(day):
            c["agents"] = [max(by_day[day], key=by_day[day].get)]
            c["attributed_by"] = "session"
    return commits

def git_commits(repo, since, until, email):
    """Your commits on the checked-out branch in the window, as attributed work units."""
    fmt = "%H%x1f%ae%x1f%aI%x1f%B%x1e"
    out = subprocess.run(["git", "-C", repo, "log", f"--since={since}T00:00:00Z", f"--until={until}T23:59:59Z", f"--format={fmt}"],
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 4 and parts[1].lower() == email.lower():
            rows.append({"source": f"local-git:{os.path.basename(os.path.abspath(repo))}", "number": parts[0][:12], "merged_at": parts[2],
                         "author": "me", "unit": "commit", "agents": attribute("", [parts[3]])})
    return rows

def pull_local(a):
    roots = [os.path.expanduser(p) for p in (a.logs or [os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude") + "/projects"])]
    lines = []
    for root in roots:
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            with open(path, errors="replace") as f:
                lines += f.readlines()
    rows, unpriced = local_usage_rows(lines, price_table(), a.since, a.until)
    write_jsonl("spend.jsonl", rows, "local")
    if unpriced:
        print(f"no list price for {', '.join(sorted(unpriced))}; those calls are left out", file=sys.stderr)
    if a.repo:
        email = a.email or subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
        commits = [c for repo in a.repo for c in attribute_by_session(git_commits(repo, a.since, a.until, email), rows, repo)]
        write_jsonl("prs.jsonl", commits, "local-git")
    print("Costs are at list price. On a Claude subscription plan you pay a flat fee instead; "
          "read the number as what the same work would cost on the API.", file=sys.stderr)

# ------------------------------------------------------------ work: GitHub ---
TRAILER = re.compile(r"^co-authored-by:\s*(.+?)\s*<([^>]*)>", re.I | re.M)
AGENT_BRANCH = re.compile(r"^(claude|codex|cursor|copilot)[/-]", re.I)

def attribute(branch, commit_messages):
    """Join keys for the agents that wrote a PR: a Claude model slug, or codex / cursor / copilot."""
    found = set()
    for msg in commit_messages:
        for name, email in TRAILER.findall(msg):
            n, e = name.lower(), email.lower()
            if "claude" in n or "anthropic" in e:
                found.add(slug(name) if any(c.isdigit() for c in name) else "claude-code")
            else:
                for tool in ("codex", "cursor", "copilot"):
                    if tool in n or tool in e:
                        found.add(tool)
    if not found and (m := AGENT_BRANCH.match(branch or "")):
        found.add("claude-code" if m.group(1).lower() == "claude" else m.group(1).lower())
    return sorted(found)

def pull_github(a):
    h = {"Authorization": f"Bearer {need('GITHUB_TOKEN', 'read access to the repo')}", "Accept": "application/vnd.github+json"}
    q = f"repo:{a.repo} is:pr is:merged merged:{a.since}..{a.until}"
    items, page = [], 1
    while True:
        res = http(f"https://api.github.com/search/issues?{urllib.parse.urlencode({'q': q, 'per_page': 100, 'page': page})}", h)
        items += res.get("items", [])
        if len(res.get("items", [])) < 100 or page == 10:
            break
        page += 1
    if res.get("total_count", 0) > len(items):
        print(f"warning: {res['total_count']} PRs merged but GitHub search returns at most 1,000; "
              f"run shorter windows (e.g. one week at a time)", file=sys.stderr)
    rows = []
    for it in items:
        pr = http(f"https://api.github.com/repos/{a.repo}/pulls/{it['number']}", h)
        commits, cp = [], 1
        while True:  # the endpoint returns at most 250 commits per PR in total
            batch = http(f"https://api.github.com/repos/{a.repo}/pulls/{it['number']}/commits?per_page=100&page={cp}", h)
            commits += batch
            if len(batch) < 100:
                break
            cp += 1
        rows.append({"source": a.repo, "number": it["number"], "merged_at": pr["merged_at"], "author": pr["user"]["login"],
                     "agents": attribute(pr["head"]["ref"], [c["commit"]["message"] for c in commits])})
    write_jsonl("prs.jsonl", rows, a.repo)

# ------------------------------------------------------------------ report ---

def load_teams(path):
    """CSV columns: email, github_login, team. Either id may be blank."""
    out = {}
    if path:
        with open(path) as f:
            rd = csv.DictReader(f)
            if "team" not in (rd.fieldnames or []):
                sys.exit(f"{path} needs a 'team' column (columns: email, github_login, team)")
            for r in rd:
                for k in ("email", "github_login"):
                    if r.get(k):
                        out[r[k].strip().lower()] = r["team"].strip()
    return out

def build_report(spend, prs, teams=None):
    teams = teams or {}
    tools, agents = defaultdict(float), defaultdict(lambda: {"cents": 0.0, "prs": 0.0, "tool": ""})
    by_team = defaultdict(lambda: {"cents": 0.0, "prs": 0, "agent_prs": 0.0})
    for r in spend:
        tools[r["tool"]] += r["cents"]
        if r["tool"] in ("Claude Code", "Cursor", "Codex", "Copilot"):
            a = agents[r["key"]]
            a["cents"] += r["cents"]; a["tool"] = r["tool"]
        if r["actor"]:
            by_team[teams.get(r["actor"].lower(), "unmapped")]["cents"] += r["cents"]
    for p in prs:
        t = by_team[teams.get(p["author"].lower(), "unmapped")]
        t["prs"] += 1
        for k in p["agents"]:
            agents[k]["prs"] += 1 / len(p["agents"])
            t["agent_prs"] += 1 / len(p["agents"])
    usd = lambda c: round(c / 100, 2)
    agent_rows = sorted(({"agent": k, "tool": v["tool"] or ("Claude Code" if k.startswith("claude") else k.title()),
                          "spend_usd": usd(v["cents"]), "merged_prs": round(v["prs"], 1),
                          "usd_per_merged_pr": round(v["cents"] / 100 / v["prs"], 2) if v["prs"] and v["cents"] else None}
                         for k, v in agents.items()), key=lambda x: -x["spend_usd"])
    total = sum(tools.values())
    agent_prs = sum(1 for p in prs if p["agents"])
    coding = sum(v["cents"] for v in agents.values())
    unit = "landed commits" if prs and all(p.get("unit") == "commit" for p in prs) else "merged PRs"
    return {
        "unit": unit, "spend_usd_total": usd(total), "merged_prs": len(prs), "merged_prs_with_agent": agent_prs,
        "agent_share": round(agent_prs / len(prs), 3) if prs else None,
        "coding_usd_per_merged_pr": round(coding / 100 / agent_prs, 2) if agent_prs and coding else None,
        "tools": sorted(({"tool": t, "spend_usd": usd(c)} for t, c in tools.items()), key=lambda x: -x["spend_usd"]),
        "agents": agent_rows,
        "teams": sorted(({"team": t, "spend_usd": usd(d["cents"]), "merged_prs": d["prs"],
                          "agent_share": round(d["agent_prs"] / d["prs"], 2) if d["prs"] else None}
                         for t, d in by_team.items()), key=lambda x: -x["spend_usd"]),
    }

def money(v, cents=True):
    return "–" if v is None else (f"${v:,.2f}" if cents else f"${v:,.0f}")

def report(a):
    out = build_report(read_jsonl("spend.jsonl"), read_jsonl("prs.jsonl"), load_teams(a.teams))
    if a.json:
        return print(json.dumps(out, indent=2))
    share = "–" if out["agent_share"] is None else f"{out['agent_share']:.0%}"
    print(f"AI spend {money(out['spend_usd_total'])} · {out['merged_prs']} {out['unit']} · {share} written with an agent · "
          f"{money(out['coding_usd_per_merged_pr'])} per agent-written {out['unit'][:-1].split()[-1]}\n")
    print("| Tool | Spend |\n|---|---:|")
    for t in out["tools"]:
        print(f"| {t['tool']} | {money(t['spend_usd'])} |")
    unit = out["unit"].split()[-1][:-1]
    print(f"\n| Coding agent · model | Spend | {out['unit'][:1].upper() + out['unit'][1:]} | Cost per {unit} |\n|---|---:|---:|---:|")
    for r in out["agents"]:
        print(f"| {r['agent']} | {money(r['spend_usd'])} | {r['merged_prs']} | {money(r['usd_per_merged_pr'])} |")
    print("\n| Team | Spend | Merged PRs | Written with an agent |\n|---|---:|---:|---:|")
    for t in out["teams"]:
        team_share = "–" if t["agent_share"] is None else f"{t['agent_share']:.0%}"
        print(f"| {t['team']} | {money(t['spend_usd'])} | {t['merged_prs']} | {team_share} |")

# --------------------------------------------------------------- dashboard ---

def dashboard_svg(out, title="SavingRabbit"):
    """A single static SVG of the report, for READMEs, Slack and slides."""
    W, ink, muted, line, accent, soft = 960, "#14201d", "#5b6763", "#dde3df", "#0f6e5c", "#dcefe9"
    esc = lambda s: html.escape(str(s))
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="620" viewBox="0 0 {W} 620" font-family="-apple-system, Segoe UI, Inter, Helvetica, Arial, sans-serif">',
         f'<rect width="{W}" height="620" rx="16" fill="#f6f7f5"/>',
         f'<text x="32" y="48" font-size="22" font-weight="700" fill="{ink}">{esc(title)}</text>',
         f'<text x="32" y="72" font-size="13" fill="{muted}">What the AI spend shipped · by tool, model and team, never by person</text>']
    share = "–" if out["agent_share"] is None else f"{out['agent_share']:.0%}"
    tiles = [("AI spend", money(out["spend_usd_total"], cents=False)), (out.get("unit", "merged PRs")[:1].upper() + out.get("unit", "merged PRs")[1:], f"{out['merged_prs']:,}"),
             ("Written with an agent", share), ("Per agent-written PR", money(out["coding_usd_per_merged_pr"]))]
    for i, (k, v) in enumerate(tiles):
        x = 32 + i * 228
        hl = i == 3
        s += [f'<rect x="{x}" y="92" width="212" height="84" rx="10" fill="{soft if hl else "#ffffff"}" stroke="{"none" if hl else line}"/>',
              f'<text x="{x + 16}" y="118" font-size="12" fill="{muted}">{esc(k)}</text>',
              f'<text x="{x + 16}" y="156" font-size="28" font-weight="700" fill="{accent if hl else ink}">{esc(v)}</text>']
    # spend by tool (left)
    s += [f'<rect x="32" y="196" width="440" height="392" rx="10" fill="#ffffff" stroke="{line}"/>',
          f'<text x="48" y="226" font-size="14" font-weight="600" fill="{ink}">Spend by tool</text>']
    tools = out["tools"][:8]
    top = max([t["spend_usd"] for t in tools] or [1])
    for i, t in enumerate(tools):
        y = 252 + i * 40
        w = max(2, 120 * t["spend_usd"] / top)
        s += [f'<text x="48" y="{y + 14}" font-size="12.5" fill="{ink}">{esc(t["tool"] if len(t["tool"]) <= 22 else t["tool"][:21] + "…")}</text>',
              f'<rect x="230" y="{y + 3}" width="{w:.1f}" height="14" rx="3" fill="{accent}"/>',
              f'<text x="456" y="{y + 14}" font-size="12" fill="{muted}" text-anchor="end">{esc(money(t["spend_usd"], cents=False))}</text>']
    # cost per merged PR by agent (right)
    s += [f'<rect x="488" y="196" width="440" height="392" rx="10" fill="#ffffff" stroke="{line}"/>',
          f'<text x="504" y="226" font-size="14" font-weight="600" fill="{ink}">Cost per merged PR, by coding agent</text>',
          f'<text x="504" y="252" font-size="11" fill="{muted}">AGENT · MODEL</text>',
          f'<text x="784" y="252" font-size="11" fill="{muted}" text-anchor="end">MERGED PRS</text>',
          f'<text x="912" y="252" font-size="11" fill="{muted}" text-anchor="end">PER PR</text>']
    for i, r in enumerate(out["agents"][:8]):
        y = 284 + i * 36
        s += [f'<line x1="504" x2="912" y1="{y - 18}" y2="{y - 18}" stroke="{line}"/>',
              f'<text x="504" y="{y}" font-size="13" fill="{ink}">{esc(r["agent"])}</text>',
              f'<text x="784" y="{y}" font-size="13" fill="{ink}" text-anchor="end">{r["merged_prs"]:,.{0 if float(r["merged_prs"]).is_integer() else 1}f}</text>',
              f'<text x="912" y="{y}" font-size="13" font-weight="600" fill="{accent}" text-anchor="end">{esc(money(r["usd_per_merged_pr"]))}</text>']
    s.append("</svg>")
    return "\n".join(s)

def dashboard(a):
    out = build_report(read_jsonl("spend.jsonl"), read_jsonl("prs.jsonl"), load_teams(a.teams))
    with open(a.out, "w") as f:
        f.write(dashboard_svg(out, a.title))
    print(f"wrote {a.out}", file=sys.stderr)

# -------------------------------------------------------------------- demo ---

def demo(a):
    """Write an illustrative dataset so you can see the report and dashboard before connecting anything."""
    spend, prs = [], []
    plan = [("Claude Code", "claude-opus-5-5", 84000, 2400), ("Claude Code", "claude-fable-5-1", 22000, 600),
            ("Claude Code", "claude-sonnet-5-5", 12000, 700), ("Cursor", "", 8000, 380), ("Codex", "", 15000, 700)]
    for tool, model, usd_, n in plan:
        spend.append(spend_row(a.since, "demo", tool, model, "dev@example.com", usd_ * 100))
        key = model or slug(tool)
        prs += [{"source": "demo", "number": len(prs) + i, "merged_at": a.since, "author": "dev", "agents": [key]} for i in range(n)]
    prs += [{"source": "demo", "number": len(prs) + i, "merged_at": a.since, "author": "dev", "agents": []} for i in range(1450)]
    for tool, usd_ in [("Claude API · pr-review-bot", 34000), ("Claude API · test-gen", 22000), ("Claude API · incident-summaries", 16000),
                       ("BlockRun gateway", 9000), ("Copilot", 2300)]:
        spend.append(spend_row(a.since, "demo", tool, "", None, usd_ * 100))
    write_jsonl("spend.jsonl", spend, "demo")
    write_jsonl("prs.jsonl", prs, "demo")

# ------------------------------------------- act on it: route and replay ---
# Samples are JSONL: {"id", "prompt", "system"?, "model" (what you use today), "accepted"? (the output you kept)}

def read_samples(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]

def blockrun_client(max_usd=None):
    try:
        from blockrun_llm import LLMClient
    except ImportError:
        sys.exit("pip install blockrun-llm   (BlockRun's SDK; it carries router-core)")
    need("BLOCKRUN_API_KEY", "brk_..., from https://user.blockrun.ai/dashboard/keys")
    return LLMClient(max_session_cost=max_usd) if max_usd else LLMClient()

def summarize_routes(decisions):
    moved = [s for s, d in decisions if s.get("model") and slug(d["model"]) != slug(s["model"])]
    est, base = sum(d["cost_estimate"] for _, d in decisions), sum(d["baseline_cost"] for _, d in decisions)
    picks = defaultdict(int)
    for _, d in decisions:
        picks[d["model"]] += 1
    return {"tasks": len(decisions), "moved_off_current_model": len(moved), "estimated_cost_usd": round(est, 4),
            "baseline_cost_usd": round(base, 4), "estimated_savings": round(1 - est / base, 3) if base else None,
            "picks": dict(sorted(picks.items(), key=lambda x: -x[1]))}

def route_cmd(a, client=None):
    """Which model BlockRun's router would pick for each sample. Local: no model is called."""
    client = client or blockrun_client()
    out = []
    for s in read_samples(a.sample):
        d = client.route(s["prompt"], system=s.get("system"), routing_profile=a.profile)
        out.append((s, d.model_dump() if hasattr(d, "model_dump") else dict(d)))
    print(json.dumps(summarize_routes(out), indent=2))
    write_jsonl("routes.jsonl", [{"id": s.get("id"), "current": s.get("model"), **d} for s, d in out])

def grade(output, accepted, how):
    if accepted is None or how == "none":
        return None
    norm = lambda t: " ".join(str(t).split()).lower()
    return norm(accepted) == norm(output) if how == "exact" else norm(accepted) in norm(output)

def replay_cmd(a, client=None):
    """Re-run each sample on each candidate model through one BlockRun API key, record what it
    cost, and check it against the output you accepted. Use samples with secrets removed."""
    client = client or blockrun_client(a.max_usd)
    rows = []
    for s in read_samples(a.sample):
        msgs = ([{"role": "system", "content": s["system"]}] if s.get("system") else []) + [{"role": "user", "content": s["prompt"]}]
        for m in a.models.split(","):
            before = client.get_spending()["total_usd"]
            r = client.chat_completion(m, msgs, max_tokens=a.max_tokens)
            text = r.choices[0].message.content or ""
            rows.append({"id": s.get("id"), "model": m, "cost_usd": client.get_spending()["total_usd"] - before,
                         "pass": grade(text, s.get("accepted"), a.grade), "output": text})
    write_jsonl("replay.jsonl", rows)
    by = defaultdict(lambda: {"n": 0, "cost": 0.0, "graded": 0, "passed": 0})
    for r in rows:
        b = by[r["model"]]; b["n"] += 1; b["cost"] += r["cost_usd"]
        if r["pass"] is not None:
            b["graded"] += 1; b["passed"] += r["pass"]
    print("| Model | Tasks | Cost per task | Matches accepted output |\n|---|---:|---:|---:|")
    for m, b in sorted(by.items(), key=lambda x: x[1]["cost"]):
        matched = f"{b['passed']}/{b['graded']}" if b["graded"] else "review by hand"
        print(f"| {m} | {b['n']} | ${b['cost'] / b['n']:.4f} | {matched} |")


def recommend(rows, tolerance=0.03):
    """Replay rows -> the cheapest model that holds quality, models to exclude, and a ClawRouter profile.
    The bar is the pass rate of the most expensive model tested (today's quality), minus a tolerance."""
    by = defaultdict(lambda: {"cost": 0.0, "n": 0, "graded": 0, "passed": 0})
    for r in rows:
        b = by[r["model"]]; b["cost"] += r["cost_usd"]; b["n"] += 1
        if r.get("pass") is not None:
            b["graded"] += 1; b["passed"] += r["pass"]
    stats = {m: {"cost_per_task": b["cost"] / b["n"], "pass_rate": b["passed"] / b["graded"] if b["graded"] else None}
             for m, b in by.items()}
    graded = {m: v for m, v in stats.items() if v["pass_rate"] is not None}
    if not graded:
        return {"error": "no graded replays; run replay with --grade exact or contains"}
    top = max(graded, key=lambda m: graded[m]["cost_per_task"])
    bar = graded[top]["pass_rate"] - tolerance
    ok = sorted((m for m, v in graded.items() if v["pass_rate"] >= bar), key=lambda m: graded[m]["cost_per_task"])
    pick = ok[0]
    saving = 1 - graded[pick]["cost_per_task"] / graded[top]["cost_per_task"] if graded[top]["cost_per_task"] else 0
    return {"bar": round(bar, 3), "current": top, "pick": pick, "saving": round(saving, 3),
            "exclude": sorted(m for m, v in graded.items() if v["pass_rate"] < bar),
            "clawrouter_profile": "premium" if pick == top else "auto", "models": stats}

def recommend_cmd(a):
    out = recommend(read_jsonl("replay.jsonl"), a.tolerance)
    if "error" in out:
        sys.exit(out["error"])
    path = os.path.join(DATA, "clawrouter-exclude-models.json")
    with open(path, "w") as f:
        json.dump(out["exclude"], f, indent=2)
    print(f"Holds quality (bar {out['bar']:.0%}): {out['pick']}, {out['saving']:.0%} cheaper per task than {out['current']}.")
    print(f"\nIn ClawRouter (https://github.com/BlockRunAI/ClawRouter):\n  /model {out['clawrouter_profile']}")
    for m in out["exclude"]:
        print(f"  /exclude add {m}")
    print(f"\nOr copy {path} to ~/.openclaw/blockrun/exclude-models.json")

# --------------------------------------------------------------------- cli ---

def main(argv=None):
    p = argparse.ArgumentParser(prog="savingrabbit", description="What your AI coding spend shipped.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    def window(sp):
        sp.add_argument("--since", required=True, help="YYYY-MM-DD"); sp.add_argument("--until", required=True, help="YYYY-MM-DD")
        return sp
    lc = window(sub.add_parser("pull-local", help="no keys: your Claude Code logs + your commits in local repos"))
    lc.add_argument("--repo", action="append", help="local git repo (repeatable)"); lc.add_argument("--email", help="default: git config user.email")
    lc.add_argument("--logs", action="append", help="Claude Code log dir (default ~/.claude/projects)")
    window(sub.add_parser("pull-claude-code", help="Claude Code spend per person, day and model (ANTHROPIC_ADMIN_KEY)"))
    window(sub.add_parser("pull-anthropic-api", help="Claude API cost by workspace and model (ANTHROPIC_ADMIN_KEY)"))
    o = window(sub.add_parser("pull-openai", help="OpenAI cost by project and line item (OPENAI_ADMIN_KEY)"))
    o.add_argument("--label", action="append", metavar="PROJECT_ID=TOOL", help="name a project, e.g. proj_abc=Codex")
    window(sub.add_parser("pull-cursor", help="Cursor usage events with charged cents (CURSOR_ADMIN_KEY)"))
    c = window(sub.add_parser("pull-copilot", help="Copilot seats x price (GITHUB_TOKEN)"))
    c.add_argument("--org", required=True); c.add_argument("--seat-price", type=float, default=19.0)
    b = window(sub.add_parser("pull-blockrun", help="BlockRun gateway spend for a wallet"))
    b.add_argument("--wallet", required=True)
    g = window(sub.add_parser("pull-github", help="merged PRs and the agents that wrote them (GITHUB_TOKEN)"))
    g.add_argument("--repo", required=True)
    l = sub.add_parser("import-log", help="your own LLM proxy log (JSONL or CSV), tagged by client")
    l.add_argument("--file", required=True)
    for f, d in [("ua", "user_agent"), ("cost", "cost_usd"), ("model", "model"), ("time", "timestamp"), ("user", "user")]:
        l.add_argument(f"--{f}-field", default=d)
    r = sub.add_parser("report", help="print the report"); r.add_argument("--teams"); r.add_argument("--json", action="store_true")
    d = sub.add_parser("dashboard", help="write the report as one SVG")
    d.add_argument("--out", default="savingrabbit.svg"); d.add_argument("--teams"); d.add_argument("--title", default="SavingRabbit")
    window(sub.add_parser("demo", help="write an illustrative dataset"))
    ro = sub.add_parser("route", help="which model BlockRun's router would pick per sample (no model call)")
    ro.add_argument("--sample", required=True); ro.add_argument("--profile", default="auto", choices=["eco", "auto", "premium"])
    rp = sub.add_parser("replay", help="re-run samples on candidate models with one BlockRun key and check them")
    rp.add_argument("--sample", required=True); rp.add_argument("--models", required=True)
    rp.add_argument("--grade", default="contains", choices=["exact", "contains", "none"])
    rp.add_argument("--max-tokens", type=int, default=1024); rp.add_argument("--max-usd", type=float, default=5.0)
    rc = sub.add_parser("recommend", help="turn replay results into a ClawRouter profile and exclude list")
    rc.add_argument("--tolerance", type=float, default=0.03)
    a = p.parse_args(argv)
    {"pull-local": pull_local, "recommend": recommend_cmd, "pull-claude-code": pull_claude_code, "pull-anthropic-api": pull_anthropic_api, "pull-openai": pull_openai,
     "pull-cursor": pull_cursor, "pull-copilot": pull_copilot, "pull-blockrun": pull_blockrun, "pull-github": pull_github,
     "import-log": import_log, "report": report, "dashboard": dashboard, "demo": demo,
     "route": route_cmd, "replay": replay_cmd}[a.cmd](a)

if __name__ == "__main__":
    main()
