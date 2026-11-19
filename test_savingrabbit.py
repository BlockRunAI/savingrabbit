"""Self-check for parsing, attribution and the join. Run: python3 test_savingrabbit.py"""
import json, os, tempfile, types
import savingrabbit as R

def test():
    assert R.slug("Claude Opus 5.5") == R.slug("anthropic/claude-opus-5.5") == "claude-opus-5-5"

    # Claude Code Analytics record (shape from Anthropic's docs; cost in cents)
    rec = {"date": "2026-11-02T00:00:00Z", "actor": {"type": "user_actor", "email_address": "dev@example.com"},
           "model_breakdown": [{"model": "claude-opus-5-5", "estimated_cost": {"currency": "USD", "amount": 3000}},
                               {"model": "claude-sonnet-5-5", "estimated_cost": {"currency": "USD", "amount": 500}}]}
    cc = R.flatten_claude_code(rec)
    assert [(r["key"], r["cents"], r["actor"]) for r in cc] == [("claude-opus-5-5", 3000, "dev@example.com"), ("claude-sonnet-5-5", 500, "dev@example.com")]

    # Anthropic cost report (amounts are cent strings), OpenAI costs (dollars), Cursor events (cents)
    ac = R.flatten_anthropic_cost({"starting_at": "2026-11-02T00:00:00Z", "results": [{"amount": "1234.5", "workspace_id": "wrkspc_1", "model": "claude-sonnet-5-5"}]})
    assert ac[0]["tool"] == "Claude API · wrkspc_1" and ac[0]["cents"] == 1234.5
    oa = R.flatten_openai_cost({"start_time": 1793577600, "results": [{"amount": {"value": 1.5, "currency": "usd"}, "project_id": "proj_a", "line_item": "gpt-6"}]}, {"proj_a": "Codex"})
    assert oa[0]["tool"] == "Codex" and oa[0]["cents"] == 150 and oa[0]["key"] == "codex"
    cu = R.flatten_cursor({"timestamp": "1793577600000", "userEmail": "dev@example.com", "model": "claude-sonnet-5-5", "chargedCents": 42})
    assert cu["tool"] == "Cursor" and cu["cents"] == 42 and cu["key"] == "cursor"
    assert R.flatten_cursor({"timestamp": "1793577600000", "tokenUsage": {"totalCents": 7}})["cents"] == 7
    br = R.flatten_blockrun({"perModel": [{"model": "anthropic/claude-sonnet-5.5", "revenueUsd": 2.5}]}, "2026-11-02")
    assert br[0]["cents"] == 250 and br[0]["tool"] == "BlockRun gateway"

    assert R.to_day("2026-11-02T10:00:00Z") == R.to_day(1793577600) == R.to_day("1793577600000") == "2026-11-02"

    # client classification uses only public user-agent prefixes
    assert R.classify_client("claude-cli/2.1.0 (external, cli)") == "Claude Code"
    assert R.classify_client("codex-tui/0.159.2 (Ubuntu; x86_64)") == "Codex"
    assert R.classify_client("opencode/1.18.30 ai-sdk/provider-utils/4.0.23") == "opencode"
    assert R.classify_client("AsyncOpenAI/Python 2.33.0") == "OpenAI SDK (Python)"
    assert R.classify_client("my-internal-thing/1.0") == "Other" and R.classify_client("") == "Unknown"

    # PR attribution
    opus = "Fix redraw\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    fable = "Add CI\n\nCo-authored-by: Claude Fable 5.1 <noreply@anthropic.com>"
    assert R.attribute("feature/x", [opus]) == ["claude-opus-5-5"]
    assert R.attribute("feature/x", [opus, fable]) == ["claude-fable-5-1", "claude-opus-5-5"]
    assert R.attribute("codex/fix-tests", ["plain"]) == ["codex"]
    assert R.attribute("x", ["Co-authored-by: Cursor Agent <cursoragent@cursor.com>"]) == ["cursor"]
    assert R.attribute("x", ["Co-authored-by: Jane Doe <jane@example.com>"]) == []

    # the join
    prs = [{"author": "dev1", "agents": ["claude-opus-5-5"]}, {"author": "dev1", "agents": ["claude-opus-5-5", "cursor"]},
           {"author": "dev2", "agents": []}]
    out = R.build_report(cc + [cu] + ac, prs, {"dev@example.com": "platform", "dev1": "platform", "dev2": "web"})
    opus_row = next(r for r in out["agents"] if r["agent"] == "claude-opus-5-5")
    assert opus_row["merged_prs"] == 1.5 and opus_row["usd_per_merged_pr"] == 20.0      # $30 / 1.5 PRs
    assert next(r for r in out["agents"] if r["agent"] == "cursor")["usd_per_merged_pr"] == 0.84  # $0.42 / 0.5 PR
    assert out["agent_share"] == 0.667 and out["merged_prs_with_agent"] == 2
    assert [t["tool"] for t in out["tools"]] == ["Claude Code", "Claude API · wrkspc_1", "Cursor"]
    assert next(t for t in out["teams"] if t["team"] == "platform")["agent_share"] == 1.0

    # dashboard renders, and the demo dataset round-trips
    svg = R.dashboard_svg(out)
    assert svg.startswith("<svg") and "claude-opus-5-5" in svg and "$20.00" in svg
    d = tempfile.mkdtemp(); R.DATA = d
    R.demo(types.SimpleNamespace(since="2026-11-01", until="2026-11-30"))
    demo_out = R.build_report(R.read_jsonl("spend.jsonl"), R.read_jsonl("prs.jsonl"))
    assert demo_out["spend_usd_total"] == 224300.0 and demo_out["merged_prs"] == 6230
    assert next(r for r in demo_out["agents"] if r["agent"] == "claude-opus-5-5")["usd_per_merged_pr"] == 35.0

    # personal mode: dedupe streamed duplicates, price at list price, prefix-match dated model ids
    prices = {"claude-opus-5-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}}
    line = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "requestId": "r1",
                       "message": {"id": "m1", "model": "claude-opus-5-5-20261101", "usage": {"input_tokens": 1000000, "output_tokens": 100000}}})
    other = json.dumps({"type": "user", "message": {"content": "secret prompt"}})
    unknown = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "message": {"id": "m2", "model": "mystery-1", "usage": {"input_tokens": 5}}})
    rows, unpriced = R.local_usage_rows([line, line, other, unknown], prices, "2026-11-01", "2026-11-30")
    assert len(rows) == 1 and rows[0]["cents"] == 750  # $5 x 1M input + $25 x 0.1M output
    assert unpriced == {"mystery-1"}
    assert R.local_usage_rows([line], prices, "2026-12-01", "2026-12-31")[0] == []

    # commits without trailers are credited to the model used in that repo that day
    sp = [dict(R.spend_row("2026-11-02", "local", "Claude Code", "claude-opus-5-5", "me", 900), cwd="/tmp/app/src"),
          dict(R.spend_row("2026-11-02", "local", "Claude Code", "claude-sonnet-5-5", "me", 100), cwd="/tmp/app"),
          dict(R.spend_row("2026-11-03", "local", "Claude Code", "claude-opus-5-5", "me", 500), cwd="/tmp/other")]
    cm = R.attribute_by_session([{"merged_at": "2026-11-02T12:00:00Z", "agents": []}, {"merged_at": "2026-11-03T12:00:00Z", "agents": []},
                                 {"merged_at": "2026-11-02T13:00:00Z", "agents": ["cursor"]}], sp, "/tmp/app")
    assert [c["agents"] for c in cm] == [["claude-opus-5-5"], [], ["cursor"]] and cm[0]["attributed_by"] == "session"

    # recommend: cheapest model that holds the bar set by the most expensive one
    rep = [{"model": "opus", "cost_usd": 0.02, "pass": True}, {"model": "opus", "cost_usd": 0.02, "pass": True},
           {"model": "sonnet", "cost_usd": 0.008, "pass": True}, {"model": "sonnet", "cost_usd": 0.008, "pass": True},
           {"model": "haiku", "cost_usd": 0.002, "pass": False}, {"model": "haiku", "cost_usd": 0.002, "pass": True}]
    rc = R.recommend(rep)
    assert rc["current"] == "opus" and rc["pick"] == "sonnet" and rc["exclude"] == ["haiku"]
    assert rc["clawrouter_profile"] == "auto" and rc["saving"] == 0.6
    assert R.recommend([{"model": "x", "cost_usd": 1, "pass": None}])["error"]

    # route summary, grading, replay (fake client: cost from the spending delta)
    dec = [({"model": "anthropic/claude-opus-5.5"}, {"model": "anthropic/claude-sonnet-5.5", "cost_estimate": 0.01, "baseline_cost": 0.04}),
           ({"model": "anthropic/claude-opus-5.5"}, {"model": "anthropic/claude-opus-5.5", "cost_estimate": 0.04, "baseline_cost": 0.04})]
    s = R.summarize_routes(dec)
    assert s["moved_off_current_model"] == 1 and s["estimated_savings"] == 0.375
    assert R.grade("The answer is  42.", "the answer is 42.", "exact") and R.grade("Sure: the answer is 42", "answer is 42", "contains")
    assert R.grade("41", "42", "contains") is False and R.grade("x", None, "exact") is None

    class Fake:
        total = 0.0
        def get_spending(self): return {"total_usd": self.total}
        def chat_completion(self, model, msgs, max_tokens=None):
            self.total += 0.02 if "opus" in model else 0.005
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="the answer is 42"))])
    path = os.path.join(d, "s.jsonl")
    open(path, "w").write(json.dumps({"id": 1, "prompt": "q", "accepted": "answer is 42"}) + "\n")
    R.replay_cmd(types.SimpleNamespace(sample=path, models="opus,sonnet", grade="contains", max_tokens=10, max_usd=1), client=Fake())
    rows = R.read_jsonl("replay.jsonl")
    assert [round(r["cost_usd"], 3) for r in rows] == [0.02, 0.005] and all(r["pass"] for r in rows)
    print("ok")

if __name__ == "__main__":
    test()
