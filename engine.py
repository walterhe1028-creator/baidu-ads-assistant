"""本地报表解析和可追溯投放规则。零外部依赖。"""
import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from datetime import date

KINDS = {"keyword": "关键词", "search": "搜索词", "creative": "创意", "unit": "单元", "plan": "方案", "project": "项目", "settings": "当前设置", "leads": "线索跟进"}
LANGUAGES = ["西班牙语", "意大利语", "葡萄牙语", "阿拉伯语", "日语", "韩语", "法语", "德语", "俄语"]
DEFAULTS = {"goal": "拓量优先", "scope": "全部语种", "intent": "先清理内容错配，保留拓量机会", "cost_cap": None, "min_clicks": 20, "min_spend": 100, "min_impressions": 500, "exclude_terms": "招聘\n求职\n工资\n杂志订阅\n杂志社订阅", "confirm_target": False, "lag_days": 3}


def number(value):
    if value is None or str(value).strip() in ("", "-", "--", "∞", "NaN", "nan"):
        return None
    try:
        n = float(str(value).replace(",", "").strip().rstrip("%"))
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def safe_cell(value):
    s = "" if value is None else str(value)
    return "'" + s if s.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else s


def csv_bytes(headers, rows):
    f = io.StringIO(newline="")
    w = csv.writer(f)
    w.writerow(headers)
    for row in rows:
        w.writerow([safe_cell(v) for v in row])
    return f.getvalue().encode("utf-8-sig")


def pick(r, *keys):
    for k in keys:
        if r.get(k, "") != "":
            return r[k]
    return ""


def identify(headers):
    h = set(headers)
    if "对象类型" in h and "对象ID" in h:
        return "settings"
    if "是否有效" in h or "有效状态" in h:
        return "leads"
    if "搜索词" in h:
        return "search"
    if "创意ID" in h:
        return "creative"
    if "项目ID" in h:
        return "project"
    if "关键词/营销要点ID" in h or "关键词ID" in h:
        return "keyword"
    if "推广单元ID" in h:
        return "unit"
    if "计划/方案ID" in h or "计划ID" in h:
        return "plan"
    raise ValueError("无法识别报表。请上传百度原始CSV，或工具提供的设置/跟进表模板。")


def parse_report(name, raw):
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("单份报表请控制在30MB以内。")
    text = None
    encoding = None
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            if "\x00" in text:
                continue
            encoding = enc
            break
        except UnicodeError:
            continue
    if not encoding:
        raise ValueError("无法读取文件编码，请另存为UTF-8 CSV。")
    lines = text.splitlines(keepends=True)
    header = None
    for i, line in enumerate(lines[:60]):
        if any(k in line for k in ("日期", "对象类型", "是否有效", "有效状态")) and ("," in line or "\t" in line):
            header = i
            break
    if header is None:
        raise ValueError("前60行未找到报表表头。")
    delimiter = "\t" if lines[header].count("\t") > lines[header].count(",") else ","
    reader = csv.DictReader(io.StringIO("".join(lines[header:])), delimiter=delimiter)
    headers = [(x or "").strip().lstrip("\ufeff") for x in (reader.fieldnames or [])]
    if len(set(headers)) != len(headers) or "" in headers:
        raise ValueError("表头有空白或重复列，请先检查。")
    reader.fieldnames = headers
    kind = identify(headers)
    records, warnings = [], []
    previous_line = 1
    for row in reader:
        source_line = header + previous_line + 1
        previous_line = reader.line_num
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"第{source_line}行列数与表头不一致。")
        if not any(str(v).strip() for v in row.values()):
            continue
        if str(row.get("日期", "")).strip() in ("合计", "汇总", "总计"):
            warnings.append(f"已排除第{source_line}行汇总，避免重复累计。")
            continue
        r = {k: v.strip() for k, v in row.items()}
        r["_line"] = source_line
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", r.get("日期", ""))
        if dates:
            for d in dates:
                date.fromisoformat(d)
            r["_start"], r["_end"] = dates[0], dates[-1]
        elif kind not in ("settings", "leads"):
            raise ValueError(f"第{source_line}行日期无法识别。")
        for col in ("消费", "展现量", "展现", "点击量", "点击", "目标转化量", "留线索量"):
            if col in r:
                n = number(r[col])
                if n is None or n < 0:
                    raise ValueError(f"第{source_line}行“{col}”不是有效非负数。")
        records.append(r)
    if not records:
        raise ValueError("报表没有可用明细行。")
    if len(records) > 150000:
        raise ValueError("单份报表最多支持15万行，请缩短时间范围。")
    starts = [r["_start"] for r in records if "_start" in r]
    ends = [r["_end"] for r in records if "_end" in r]
    return {"name": name, "kind": kind, "encoding": encoding, "hash": hashlib.sha256(raw).hexdigest(), "headers": headers, "metadata": "".join(lines[:header]).strip(), "start": min(starts) if starts else "", "end": max(ends) if ends else "", "count": len(records), "records": records, "warnings": warnings}


def fields(r):
    return {"account": r.get("账户", ""), "plan": pick(r, "计划/方案", "计划", "计划名称"), "plan_id": pick(r, "计划/方案ID", "计划ID"), "unit": pick(r, "推广单元", "单元"), "unit_id": pick(r, "推广单元ID", "单元ID")}


def group_rows(report, kind, start=None, end=None):
    groups = {}
    if not report:
        return []
    for r in report["records"]:
        if start and (r.get("_start", "") < start or r.get("_end", "") > end):
            continue
        f = fields(r)
        oid = pick(r, {"keyword": "关键词/营销要点ID", "creative": "创意ID", "plan": "计划/方案ID", "unit": "推广单元ID", "project": "项目ID"}.get(kind, ""), "关键词ID")
        name = pick(r, "搜索词") if kind == "search" else pick(r, "关键词/营销要点", "关键词", "创意标题", "项目", "推广单元", "计划/方案")
        key = (f["account"], f["plan_id"], f["unit_id"], oid, name if kind == "search" or not oid else "")
        if key not in groups:
            groups[key] = {**f, "object_id": oid, "name": name, "spend": 0, "clicks": 0, "impressions": 0, "target": 0, "leads": 0, "has_target": "目标转化量" in r, "has_leads": "留线索量" in r, "sources": [], "quality": [], "latest": r}
        g = groups[key]
        g["spend"] += number(r.get("消费")) or 0
        g["clicks"] += number(pick(r, "点击量", "点击")) or 0
        g["impressions"] += number(pick(r, "展现量", "展现")) or 0
        g["target"] += number(r.get("目标转化量")) or 0
        g["leads"] += number(r.get("留线索量")) or 0
        g["sources"].append({"file": report["name"], "line": r["_line"], "start": r.get("_start"), "end": r.get("_end")})
        q = number(r.get("质量度"))
        if q is not None:
            g["quality"].append(q)
        if r.get("_end", "") >= g["latest"].get("_end", ""):
            g["latest"] = r
    for g in groups.values():
        g["spend"] = round(g["spend"], 2)
        g["ctr"] = g["clicks"] / g["impressions"] if g["impressions"] else None
        g["cpc"] = round(g["spend"] / g["clicks"], 2) if g["clicks"] else None
        g["quality"] = round(sum(g["quality"]) / len(g["quality"]), 1) if g["quality"] else None
    return list(groups.values())


def config_checked(value):
    c = {**DEFAULTS, **(value or {})}
    if c["goal"] not in ("拓量优先", "成本优先", "先清理内容"):
        raise ValueError("请选择支持的决策目标。")
    if c["scope"] not in ["全部语种"] + LANGUAGES:
        raise ValueError("请选择支持的语种范围。")
    for k, low, high in (("min_clicks", 1, 100000), ("min_spend", 0, 10000000), ("min_impressions", 1, 10000000), ("lag_days", 0, 30)):
        n = number(c[k])
        if n is None or not low <= n <= high:
            raise ValueError(f"参数 {k} 超出范围。")
        c[k] = n
    c["cost_cap"] = number(c["cost_cap"])
    if c["cost_cap"] is not None and c["cost_cap"] <= 0:
        raise ValueError("成本上限必须大于0，也可以留空。")
    c["intent"] = str(c["intent"])[:2000]
    c["exclude_terms"] = str(c["exclude_terms"])[:4000]
    c["confirm_target"] = c["confirm_target"] is True
    return c


def analyze(reports, config=None):
    c = config_checked(config)
    daily = [r for k, r in reports.items() if k not in ("creative", "settings", "leads") and r["start"]]
    start = max((r["start"] for r in daily), default="")
    end = min((r["end"] for r in daily), default="")
    if daily and start > end:
        raise ValueError("各报表没有共同日期范围，请上传覆盖相同日期的报表。")
    aligned = {k: group_rows(reports.get(k), k, start, end) for k in ("keyword", "search", "unit", "plan", "project")}
    creatives = group_rows(reports.get("creative"), "creative")
    primary_kind = next((k for k in ("project", "plan", "keyword", "unit") if aligned[k]), None)
    primary = aligned.get(primary_kind, [])
    spend = round(sum(x["spend"] for x in primary), 2)
    clicks = int(sum(x["clicks"] for x in primary))
    impressions = int(sum(x["impressions"] for x in primary))
    project_targets = sum(x["target"] for x in aligned["project"]) if aligned["project"] else None
    search_leads = sum(x["leads"] for x in aligned["search"]) if reports.get("search") and "留线索量" in reports["search"]["headers"] else None
    alerts = []
    if len({(r["start"], r["end"]) for r in daily}) > 1:
        alerts.append({"level": "warning", "text": f"日期已对齐至 {start}—{end}。创意合计保留原始周期，不参与共同周期转化归因。"})
    alerts.append({"level": "warning", "text": "没有客户级归因，无法计算关键词/搜索词/创意的有效线索成本。有效线索与留电话、目标转化分开显示。"})
    if project_targets and search_leads == 0:
        alerts.append({"level": "danger", "text": f"共同周期项目目标转化为{project_targets:g}，搜索词留线索为0。事件口径或回传覆盖待核实；禁止据此按“无转化”批量停词。"})
    if not c["confirm_target"]:
        alerts.append({"level": "warning", "text": "项目“目标转化”尚未确认等于留电话；当前只显示每次目标转化成本，不标记为留电成本。"})
    modes = sorted(set(r.get("出价模式", "") for r in reports.get("project", {}).get("records", [])) - {""})
    if any("目标转化" in m or "oCPC" in m for m in modes):
        alerts.append({"level": "info", "text": "当前项目为智能出价。平均点击价格不等于关键词出价；逐词改价需先核实该层级是否支持。"})
    settings = {}
    duplicate_settings = set()
    for r in reports.get("settings", {}).get("records", []):
        key = (r.get("对象类型"), r.get("对象ID"))
        if key in settings:
            duplicate_settings.add(key)
        settings[key] = r
    if duplicate_settings:
        alerts.append({"level": "danger", "text": "设置快照存在重复对象ID，相关修改前值将标记为不确定。"})
    units_by_keyword = defaultdict(dict)
    for g in aligned["search"]:
        if g["unit_id"]:
            units_by_keyword[(g["plan_id"], g["object_id"])][g["unit_id"]] = g["unit"]
    keywords = aligned["keyword"]
    existing_words = {(x["plan_id"], re.sub(r"\s+", "", x["name"]).lower()) for x in keywords}
    recs = []

    def add(g, kind, action, field, before, after, reason, priority=2, blockers=None, caution="", key_extra=""):
        if c["scope"] != "全部语种" and c["scope"] not in g["plan"]:
            return
        key = "|".join([kind, g.get("account", ""), g["plan_id"], g["unit_id"], g["object_id"], g["name"], action, key_extra])
        rid = hashlib.sha256(key.encode()).hexdigest()[:18]
        source_starts = [s["start"] for s in g["sources"] if s.get("start")]
        source_ends = [s["end"] for s in g["sources"] if s.get("end")]
        recs.append({"id": rid, "kind": kind, "type": KINDS[kind], "plan": g["plan"], "plan_id": g["plan_id"], "unit": g["unit"], "unit_id": g["unit_id"], "object_id": g["object_id"], "object": g["name"], "action": action, "field": field, "before": before, "after": after, "reason": reason, "priority": priority, "blockers": blockers or [], "caution": caution, "spend": g["spend"], "clicks": g["clicks"], "impressions": g["impressions"], "cpc": g["cpc"], "sources": g["sources"], "basis": "内容/相关性" if kind in ("search", "creative") else "流量/设置", "period": f'{min(source_starts) if source_starts else start}—{max(source_ends) if source_ends else end}'})

    excludes = [x.strip() for x in re.split(r"[\n,，;；]", c["exclude_terms"]) if x.strip()]
    search_candidates = {}
    for g in aligned["search"]:
        key = (g["plan_id"], g["unit_id"], g["name"].lower())
        if key not in search_candidates:
            search_candidates[key] = {**g, "sources": list(g["sources"]), "object_id": ""}
        else:
            item = search_candidates[key]
            for col in ("spend", "clicks", "impressions"):
                item[col] += g[col]
            item["spend"] = round(item["spend"], 2)
            item["cpc"] = round(item["spend"] / item["clicks"], 2) if item["clicks"] else None
            item["sources"].extend(g["sources"])
            if g["latest"].get("_end", "") > item["latest"].get("_end", "") or g["latest"].get("_end", "") == item["latest"].get("_end", "") and g["latest"].get("账户添加状态") == "已添加":
                item["latest"] = g["latest"]
    for g in search_candidates.values():
        term = g["name"]
        matches = [x for x in excludes if x.lower() in term.lower()]
        if matches:
            add(g, "search", "添加精确否词", "精确否定关键词", "未提供现有否词表", term, f"命中你设置的排除词：{'、'.join(matches)}。仅建议精确否定该搜索词。", 1, ["核对现有否词及生效层级"], "这是意图规则，不是低转化判定。")
        elif c["goal"] != "先清理内容" and g["clicks"] >= max(3, c["min_clicks"] / 5) and re.search(r"培训|课程|报名|学费|机构|辅导|一对一|考级", term) and len(term) <= 40 and g["latest"].get("账户添加状态") == "未添加" and (g["plan_id"], re.sub(r"\s+", "", term).lower()) not in existing_words:
            add(g, "search", "新增关键词测试", "关键词（匹配待确认）", "未添加（报表状态）", term, f"有课程咨询意图，累计{g['clicks']:g}次点击，尚未添加到本计划。可纳入小额测试。", 3, ["确认匹配方式、测试预算及支持的导入层级"], "没有有效线索归因，不代表该词已验证有效；未预设测试预算。")

    unit_totals = defaultdict(lambda: [0, 0])
    for g in creatives:
        unit_totals[(g["plan_id"], g["unit_id"])][0] += g["clicks"]
        unit_totals[(g["plan_id"], g["unit_id"])][1] += g["impressions"]
    for g in creatives:
        r = g["latest"]
        desc = pick(r, "创意描述", "创意描述1")
        text = g["name"] + " " + desc
        lang = next((x for x in LANGUAGES if x in g["plan"]), None)
        flags = []
        other = [x for x in LANGUAGES if lang and x != lang and x in desc]
        if other:
            flags.append(f"{lang}计划的描述出现{'、'.join(other)}，需检查课程错配")
        if re.search(r"杂志社|杂志订阅|订阅杂志|杂志之家", text):
            flags.append("课程推广内容包含杂志订阅信息")
        if "樱花" in text and "新东方" in text:
            flags.append("同一创意混用樱花与新东方品牌，需核实品牌归属")
        if flags:
            subject = lang or "小语种"
            replacement = f"成都{subject}课程咨询｜了解适合自己的学习安排"
            description = f"想学习{subject}？咨询课程内容、学习阶段与上课安排，结合基础和学习目标了解可选课程。"
            add(g, "creative", "改写创意", "标题及描述", json.dumps({"标题": g["name"], "描述": desc}, ensure_ascii=False), json.dumps({"标题": replacement, "描述": description}, ensure_ascii=False), "；".join(flags), 1, [], "替换文案为待审核草稿；未加入免费试听、保过或师资等未经确认的承诺。")
        else:
            uc, ui = unit_totals[(g["plan_id"], g["unit_id"])]
            peer_ctr = (uc - g["clicks"]) / (ui - g["impressions"]) if ui > g["impressions"] else None
            if g["impressions"] >= c["min_impressions"] and peer_ctr and (g["ctr"] or 0) < peer_ctr * .5:
                add(g, "creative", "创意对照测试", "待制定", "现有创意", "补充一个不同诉求版本，保留原版对照", f"该创意CTR为{g['ctr']:.2%}，同单元其他创意合计CTR为{peer_ctr:.2%}；未控制排名、设备等因素。", 3, ["没有创意级转化，不能仅凭CTR直接暂停"], "比较周期为创意原始合计周期。")
    for g in keywords:
        mapping = units_by_keyword.get((g["plan_id"], g["object_id"]), {})
        if len(mapping) == 1:
            g["unit_id"], g["unit"] = next(iter(mapping.items()))
        if g["quality"] is not None and g["quality"] <= 3 and g["impressions"] >= c["min_impressions"]:
            add(g, "keyword", "优化相关性", "待制定", f"日质量度简单均值 {g['quality']:g}", "检查关键词、创意、落地页的一致性", "质量度偏低且已有一定展现。质量度均值仅作诊断线索，不作为最新质量度。", 2, ["需核对最新质量度和落地页"])
        elif c["goal"] != "先清理内容" and g["spend"] >= c["min_spend"] and g["clicks"] >= c["min_clicks"]:
            add(g, "keyword", "核查高消耗词", "待制定", f"消费 ¥{g['spend']:.2f}", "先补齐转化事件或检查搜索词结构", f"达到你设置的消费¥{c['min_spend']:g}、点击{c['min_clicks']:g}诊断阈值；关键词报表没有转化列。", 2, ["关键词级转化和当前设置缺失，不生成停词或改价"], "高消费不等于浪费；尤其不能用平均CPC代替当前出价。")
    for g in aligned["plan"]:
        snapshot = settings.get(("方案", g["object_id"])) or settings.get(("计划", g["object_id"]))
        add(g, "plan", "计划设置待补充", "预算/地域/时段", json.dumps(snapshot, ensure_ascii=False) if snapshot else "未提供当前设置", "补充预算、目标CPA、地域、时段、状态后制定调整", "有计划消费和流量，但没有计划转化及设置快照，无法据此分配有效线索拓量预算。", 3, ["计划级转化、当前设置及成本边界待补充"])

    recs.sort(key=lambda x: (x["priority"], -x["spend"], x["id"]))
    if not reports.get("settings"):
        alerts.append({"level": "warning", "text": "尚无当前设置快照。出价、预算、状态、匹配、地域和时段的修改前值不可推断；可在清单内提出建议并补充前值。"})
    if not c["cost_cap"]:
        alerts.append({"level": "info", "text": "尚未设置可接受的留电成本上限，不生成加价或增加预算建议。"})
    report_info = []
    for k, r in reports.items():
        original = group_rows(r, k) if k not in ("settings", "leads") else []
        report_info.append({key: r[key] for key in ("name", "kind", "encoding", "hash", "metadata", "start", "end", "count", "headers", "warnings")})
        report_info[-1]["spend"] = round(sum(x["spend"] for x in original), 2) if original else None
    revision = hashlib.sha256(json.dumps({"data": {k: r["hash"] for k, r in sorted(reports.items())}, "config": c, "engine": 1}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    recent = []
    if primary_kind:
        by_date = defaultdict(lambda: {"spend": 0, "clicks": 0, "target": 0})
        for r in reports[primary_kind]["records"]:
            d = r.get("_start", "")
            if start <= d <= end:
                by_date[d]["spend"] += number(r.get("消费")) or 0
                by_date[d]["clicks"] += number(pick(r, "点击量", "点击")) or 0
                by_date[d]["target"] += number(r.get("目标转化量")) or 0
        recent = [{"date": d, **{k: round(v, 2) for k, v in x.items()}} for d, x in sorted(by_date.items())]
    leads_info = None
    if reports.get("leads"):
        rr = reports["leads"]["records"]
        valid = sum(pick(r, "是否有效", "有效状态") in ("是", "有效", "1", "true") for r in rr)
        classified = sum(pick(r, "是否有效", "有效状态") in ("是", "有效", "1", "true", "否", "无效", "0", "false") for r in rr)
        leads_info = {"total": len(rr), "classified": classified, "valid": valid, "rate": valid / classified if classified else None, "note": "仅跟进表已判定记录有效率；未与广告分母、去重或归因核对，不外推关键词有效率。"}
    return {"revision": revision, "config": c, "period": {"start": start, "end": end}, "summary": {"spend": spend, "clicks": clicks, "impressions": impressions, "target": project_targets, "search_leads": search_leads, "target_cpa": round(spend / project_targets, 2) if project_targets else None, "cpc": round(spend / clicks, 2) if clicks else None, "ctr": clicks / impressions if impressions else None, "primary": KINDS.get(primary_kind, "无"), "modes": modes, "effective_leads": None}, "alerts": alerts, "reports": report_info, "recommendations": recs, "plans": aligned["plan"], "trend": recent, "leads_info": leads_info, "model": {"connected": False, "label": "规则引擎已运行 · 大模型未连接"}}


REVIEW_HEADERS = ["建议ID", "对象类型", "计划", "计划ID", "单元", "单元ID", "对象", "对象ID", "建议动作", "修改字段", "修改前", "修改后", "判断依据", "来源周期消费", "点击", "展现", "待补充条件", "注意事项", "来源", "规则版本"]


def review_rows(analysis, selected, edits):
    if len(selected) != len(set(selected)):
        raise ValueError("存在重复选择。")
    known = {r["id"]: r for r in analysis["recommendations"]}
    if any(rid not in known for rid in selected):
        raise ValueError("建议已更新，请刷新后重新选择。")
    output = []
    for rid in selected:
        r = known[rid]
        e = edits.get(rid, {})
        after = str(e.get("after", r["after"]))[:10000]
        note = str(e.get("note", ""))[:2000]
        if not after.strip():
            raise ValueError("修改后内容不能为空。")
        output.append([rid, r["type"], r["plan"], r["plan_id"], r["unit"], r["unit_id"], r["object"], r["object_id"], r["action"], r["field"], r["before"], after, r["reason"], r["spend"], r["clicks"], r["impressions"], "；".join(r["blockers"]), r["caution"] + "；指标周期：" + r["period"] + ("；人工备注：" + note if note else ""), "；".join(f'{s["file"]}:L{s["line"]}' for s in r["sources"]), analysis["revision"]])
    return output
