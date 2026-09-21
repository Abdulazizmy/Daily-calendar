#!/usr/bin/env python3
"""
تحديث المباريات تلقائياً من ESPN وكتابتها في matches.json

البطولات (تقدر تعدّل القائمة COMPETITIONS تحت):
  - دوري روشن السعودي         (كامل: 306 مباراة)
  - دوري أبطال آسيا للنخبة    (مباريات الفرق السعودية فقط)
  - دوري أبطال آسيا 2         (مباريات الفرق السعودية فقط)
  - كأس الملك                 (مباريات الفرق السعودية فقط)

بطولات ما يغطيها ESPN (مثل كأس السوبر) تنكتب يدوياً في extra-matches.json.

تفاصيل المباريات المنتهية (المسجلين والبطاقات والصناعة) تنكتب في match-details.json.
هذي التفاصيل إضافية: إذا فشل جلبها ما يتأثر جدول المباريات.

الأمان: إذا فشل أي فحص للدوري ما يكتب أي شي، ويبقى الملف القديم سليم.

الاستخدام:
    python scripts/update_matches.py            # تحديث الملفات
    python scripts/update_matches.py --dry-run  # عرض النتيجة بدون كتابة
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

CORE = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/"
LOGO = "https://a.espncdn.com/i/teamlogos/soccer/500/{}.png"
RIYADH = dt.timezone(dt.timedelta(hours=3))
USER_AGENT = "Mozilla/5.0 (compatible; daily-calendar-updater)"
WORKERS = 5
DETAILS_BUDGET = 420      # ثواني نخصصها لجلب الصناعة في التشغيل الواحد
ASSIST_TRIES = 3          # كم مرة نعيد محاولة الصناعة لمباراة قبل ما نتركها

# strict=True: الدوري لازم يكتمل ويعدي الفحص، وإلا ما نكتب شي
COMPETITIONS = [
    {"slug": "ksa.1", "name": "الدوري السعودي", "strict": True},
    {"slug": "afc.champions", "name": "دوري أبطال آسيا للنخبة", "strict": False},
    {"slug": "afc.cup", "name": "دوري أبطال آسيا 2", "strict": False},
    {"slug": "ksa.kings.cup", "name": "كأس الملك", "strict": False},
]


class UpdateError(Exception):
    """خطأ يوقف التحديث بدون ما نلمس الملفات."""


class Undecided(UpdateError):
    """مباراة فرقها ما تحددت بعد (أدوار إقصائية)."""


# ---------------------------------------------------------------- الشبكة

def fetch_json(url, retries=4):
    last = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )

            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as error:
            last = error

            if error.code in (400, 401, 403, 404):
                break

        except Exception as error:  # noqa: BLE001
            last = error

        time.sleep(1.5 * (attempt + 1))

    raise UpdateError(f"فشل جلب {url}: {last}")


# نخليها متغير عشان نقدر نبدلها عند الاختبار
FETCH = fetch_json


def resolve(value):
    """ESPN يرجع أحياناً {"$ref": رابط} بدل البيانات نفسها."""
    if (
        isinstance(value, dict)
        and "$ref" in value
        and not any(key in value for key in ("type", "value", "displayValue", "competitors"))
    ):
        return FETCH(value["$ref"])

    return value


def map_parallel(function, items):
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(function, items))


def safe(function, item):
    """ينفّذ الدالة ويرجع (النتيجة, الخطأ) بدل ما يوقف كل شي."""
    try:
        return function(item), None
    except UpdateError as error:
        return None, error
    except Exception as error:  # noqa: BLE001
        return None, UpdateError(f"{type(error).__name__}: {error}")


# ---------------------------------------------------------------- تحليل البيانات

def parse_date(text):
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?Z?$",
        str(text),
    )

    if not match:
        raise UpdateError(f"صيغة تاريخ غير معروفة: {text!r}")

    year, month, day, hour, minute, second = (int(x or 0) for x in match.groups())

    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)


def team_ref_and_id(competitor):
    """يرجع (رابط بيانات الفريق, رقم الفريق) من طرف المباراة."""
    team = competitor.get("team")
    ref = team.get("$ref") if isinstance(team, dict) else None

    if ref:
        found = re.search(r"/teams/(\d+)", ref)

        if found:
            return ref, found.group(1)

    if isinstance(team, dict) and team.get("id"):
        return ref, str(team["id"])

    return ref, str(competitor.get("id"))


def parse_event(event):
    """يطلع من الحدث: الوقت والفريقين والملعب ومراجع الحالة والنتيجة."""
    competitions = event.get("competitions") or []

    if not competitions:
        raise UpdateError(f"الحدث {event.get('id')} بدون competitions")

    competition = resolve(competitions[0])
    competitors = competition.get("competitors") or []

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

    if not home or not away:
        raise UpdateError(f"الحدث {event.get('id')} ما فيه فريق ضيف ومضيف")

    _, home_id = team_ref_and_id(home)
    _, away_id = team_ref_and_id(away)

    if not home_id.isdigit() or not away_id.isdigit():
        raise Undecided(f"الحدث {event.get('id')} فريقه غير محدد بعد")

    venue = competition.get("venue")
    venue_name = (venue.get("fullName") or "") if isinstance(venue, dict) else ""

    return {
        "id": str(event.get("id") or competition.get("id")),
        "start": parse_date(competition.get("date") or event.get("date")),
        "home_id": home_id,
        "away_id": away_id,
        "home_team": home.get("team"),
        "away_team": away.get("team"),
        "venue": venue_name,
        "status": competition.get("status") or event.get("status"),
        "home_competitor": home,
        "away_competitor": away,
    }


def read_status(status):
    """يرجع: scheduled / live / final / dropped"""
    status = resolve(status)

    if not isinstance(status, dict):
        return "scheduled"

    kind = status.get("type") or {}
    name = str(kind.get("name", "")).upper()
    state = str(kind.get("state", "")).lower()

    if any(word in name for word in ("POSTPONED", "CANCEL", "ABANDON", "FORFEIT")):
        return "dropped"

    if kind.get("completed") is True or state == "post":
        return "final"

    if state == "in":
        return "live"

    return "scheduled"


def read_score(competitor):
    score = resolve(competitor.get("score"))

    if isinstance(score, dict):
        value = score.get("value", score.get("displayValue"))
    else:
        value = score

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- أسماء الأندية الأجنبية

def normalize(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    skip = {"al", "el", "fc", "sc", "cf", "ac", "club", "the"}

    return " ".join(w for w in words if w not in skip and len(w) > 1)


def foreign_name(team_id, team_obj, arabic_names, cache):
    """يرجع (الاسم, ما لقينا له اسم عربي؟) للنادي الأجنبي."""
    if team_id in cache:
        return cache[team_id]

    info = None

    if isinstance(team_obj, dict):
        if team_obj.get("displayName"):
            info = team_obj
        elif team_obj.get("$ref"):
            try:
                info = FETCH(team_obj["$ref"])
            except UpdateError:
                info = None

    english = ""
    result = None

    if isinstance(info, dict):
        english = info.get("displayName") or info.get("name") or ""

        for key in ("displayName", "name", "location", "shortDisplayName"):
            value = info.get(key)

            if value and normalize(value) in arabic_names:
                result = arabic_names[normalize(value)]
                break

    if result is None and team_id in arabic_names:
        result = arabic_names[team_id]

    cache[team_id] = (result or english or f"فريق {team_id}", result is None)

    return cache[team_id]


# ---------------------------------------------------------------- الملفات

def read_json(path, default):
    if not os.path.exists(path):
        return default

    with open(path, encoding="utf-8") as file:
        return json.load(file)


def read_text(path):
    if not os.path.exists(path):
        return ""

    with open(path, encoding="utf-8") as file:
        return file.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)


def dump_rows(rows):
    return "[\n" + ",\n".join(
        "  " + json.dumps(row, ensure_ascii=False) for row in rows
    ) + "\n]\n"


def iso_riyadh(moment):
    return moment.astimezone(RIYADH).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def season_year(now):
    override = os.environ.get("SEASON")

    if override:
        return int(override)

    # الموسم يبدأ في أغسطس: أغسطس 2026 → موسم 2026
    return now.year if now.month >= 7 else now.year - 1


# ---------------------------------------------------------------- تفاصيل المباريات

def team_id_from(value):
    if isinstance(value, dict):
        found = re.search(r"/teams/(\d+)", str(value.get("$ref") or ""))

        if found:
            return found.group(1)

        if value.get("id"):
            return str(value["id"])

    return ""


def play_player(play):
    """اسم اللاعب من نص الحدث: 'Malcom (Al Hilal) Goal at 26'' """
    text = str(play.get("text") or "")
    found = re.match(r"^(.*?)\s+\(", text)

    if found and found.group(1).strip():
        return found.group(1).strip()

    short = str(play.get("shortText") or "")
    kind = str((play.get("type") or {}).get("text") or "")

    if kind and short.endswith(kind):
        short = short[: -len(kind)]

    return short.strip()


def play_minute(play):
    clock = str((play.get("clock") or {}).get("displayValue") or "")
    added = str((play.get("addedClock") or {}).get("displayValue") or "")

    if added and added not in clock:
        clock += added

    return clock


def fetch_plays(slug, event_id):
    base = f"{CORE}{slug}/events/{event_id}/competitions/{event_id}/plays?limit=300"
    first = FETCH(base)
    items = list(first.get("items") or [])

    for page in range(2, int(first.get("pageCount") or 1) + 1):
        items += FETCH(f"{base}&page={page}").get("items") or []

    return [resolve(item) for item in items]


def athlete_name(ref, cache):
    if not ref:
        return ""

    if ref not in cache:
        try:
            info = FETCH(ref)
            cache[ref] = str(info.get("displayName") or info.get("fullName") or "")
        except UpdateError:
            cache[ref] = ""

    return cache[ref]


def parse_plays(items, home_id, away_id, names_cache):
    """يطلع الأهداف والبطاقات من أحداث المباراة."""
    goals, cards = [], []
    prev_home = prev_away = 0

    ordered = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda p: (p.get("clock") or {}).get("value") or 0,
    )

    for play in ordered:
        kind = str((play.get("type") or {}).get("type") or "").lower()
        team_id = team_id_from(play.get("team"))
        own_side = "home" if team_id == home_id else ("away" if team_id == away_id else "")

        if play.get("scoringPlay") and not play.get("shootout"):
            own = bool(play.get("ownGoal")) or "own" in kind
            penalty = bool(play.get("penaltyKick")) or "penalty" in kind
            home_score, away_score = play.get("homeScore"), play.get("awayScore")
            side = ""

            # الهدف يُنسب للفريق اللي زادت نتيجته (يشمل الأهداف العكسية)
            if isinstance(home_score, int) and isinstance(away_score, int):
                if home_score > prev_home:
                    side = "home"
                elif away_score > prev_away:
                    side = "away"

                prev_home, prev_away = home_score, away_score

            if not side:
                side = own_side

                if own:
                    side = {"home": "away", "away": "home"}.get(own_side, "")

            assist = ""

            for part in play.get("participants") or []:
                if "assist" in str(part.get("type") or "").lower():
                    athlete = part.get("athlete")
                    assist = athlete_name(
                        athlete.get("$ref") if isinstance(athlete, dict) else None,
                        names_cache,
                    )

            goals.append({
                "minute": play_minute(play),
                "side": side,
                "player": play_player(play),
                "type": "own" if own else ("penalty" if penalty else "goal"),
                "assist": assist,
            })

        elif play.get("yellowCard") or play.get("redCard") or "red-card" in kind:
            red = bool(play.get("redCard")) or "red-card" in kind

            cards.append({
                "minute": play_minute(play),
                "side": own_side,
                "player": play_player(play),
                "card": "red" if red else "yellow",
            })

    return goals, cards


def stat_value(stats, wanted):
    """يقرأ إحصائية بالاسم من رد إحصائيات اللاعب."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") == wanted and "value" in node:
                found.append(node["value"])

            for child in node.values():
                walk(child)

        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(stats)

    for value in found:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue

    return 0


def fetch_team_assists(slug, event_id, team_id, names_cache):
    """قائمة (اللاعب, عدد الصناعات) للاعبي فريق في مباراة."""
    base = f"{CORE}{slug}/events/{event_id}/competitions/{event_id}/competitors/{team_id}"
    roster = FETCH(f"{base}/roster?limit=100")
    entries = roster.get("entries") or roster.get("items") or []
    jobs = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        athlete = entry.get("athlete") if isinstance(entry.get("athlete"), dict) else {}
        player_id = str(entry.get("playerId") or "")

        if not player_id:
            found = re.search(r"/athletes/(\d+)", str(athlete.get("$ref") or ""))
            player_id = found.group(1) if found else ""

        if not player_id:
            continue

        stats = entry.get("statistics")
        url = stats.get("$ref") if isinstance(stats, dict) and stats.get("$ref") else \
            f"{base}/roster/{player_id}/statistics/0"

        jobs.append((url, athlete.get("$ref"), entry.get("displayName") or athlete.get("displayName")))

    if not jobs:
        raise UpdateError("قائمة اللاعبين فاضية")

    def one(job):
        url, athlete_ref, name = job
        count = stat_value(FETCH(url), "goalAssists")

        if count > 0 and not name:
            name = athlete_name(athlete_ref, names_cache)

        return name or "", count

    results = map_parallel(lambda job: safe(one, job), jobs)
    good = [value for value, error in results if error is None]

    if not good:
        raise UpdateError("ما قدرنا نقرأ إحصائيات اللاعبين")

    return [{"player": name, "count": count} for name, count in good if count > 0]


def link_assists(entry):
    """إذا فريق سجل هدف واحد عادي وصنعه لاعب واحد بالضبط نربطهم ببعض."""
    for side in ("home", "away"):
        eligible = [g for g in entry["goals"] if g["side"] == side and g["type"] == "goal"]
        givers = [a for a in entry.get("assists", []) if a["side"] == side]

        if (
            len(eligible) == 1
            and len(givers) == 1
            and givers[0]["count"] == 1
            and not eligible[0].get("assist")
        ):
            eligible[0]["assist"] = givers[0]["player"]
            givers[0]["linked"] = True


def update_details(final_events, before, now, log):
    """يحدّث تفاصيل المباريات المنتهية. كل خطأ هنا ما يوقف شي."""
    out = {key: value for key, value in before.items()}
    names_cache = {}
    started = time.time()
    recent_from = now - dt.timedelta(hours=30)

    todo = [
        ev for ev in final_events
        if ev["key"] not in before or ev["start"] > recent_from
    ]

    def get_plays(ev):
        items = fetch_plays(ev["slug"], ev["eid"])
        return parse_plays(items, ev["home_id"], ev["away_id"], names_cache)

    results = map_parallel(lambda ev: safe(get_plays, ev), todo)
    new_count = failed = 0

    for ev, (parsed, error) in zip(todo, results):
        if error is not None:
            failed += 1
            continue

        goals, cards = parsed
        old = before.get(ev["key"]) or {}
        entry = {
            "goals": goals,
            "cards": cards,
            "assists": old.get("assists", []),
            "assistsDone": bool(old.get("assistsDone", False)),
            "assistsKnown": bool(old.get("assistsKnown", False)),
            "tries": int(old.get("tries", 0)),
        }

        if len(goals) != len(old.get("goals", goals)):
            entry.update(assists=[], assistsDone=False, assistsKnown=False, tries=0)

        if ev["key"] not in before:
            new_count += 1

        # نحافظ على ربط الصناعة القديم إذا الأهداف ما تغيرت
        for new_goal, old_goal in zip(goals, old.get("goals", [])):
            if old_goal.get("assist") and not new_goal.get("assist") \
                    and old_goal.get("player") == new_goal.get("player"):
                new_goal["assist"] = old_goal["assist"]

        out[ev["key"]] = entry

    # --- الصناعة: نبدأ بالأحدث، وبميزانية وقت
    pending = sorted(
        (ev for ev in final_events if ev["key"] in out and not out[ev["key"]]["assistsDone"]),
        key=lambda ev: ev["start"],
        reverse=True,
    )
    done_now = 0

    for ev in pending:
        if time.time() - started > DETAILS_BUDGET:
            break

        entry = out[ev["key"]]
        found = []

        try:
            for side, team_id in (("home", ev["home_id"]), ("away", ev["away_id"])):
                open_goals = [g for g in entry["goals"] if g["side"] == side and g["type"] == "goal"]

                if not open_goals:
                    continue

                for item in fetch_team_assists(ev["slug"], ev["eid"], team_id, names_cache):
                    found.append({"side": side, **item})

            entry["assists"] = found
            entry["assistsKnown"] = True
            entry["assistsDone"] = True
            link_assists(entry)
            done_now += 1

        except UpdateError:
            entry["tries"] = entry.get("tries", 0) + 1

            if entry["tries"] >= ASSIST_TRIES:
                entry["assistsDone"] = True   # ESPN ما يوفرها: نوقف المحاولة

    left = sum(1 for ev in final_events if ev["key"] in out and not out[ev["key"]]["assistsDone"])
    unknown = sum(1 for ev in final_events if ev["key"] in out and not out[ev["key"]]["assistsKnown"]
                  and out[ev["key"]]["assistsDone"])

    log(
        f"تفاصيل المباريات: {len(final_events)} منتهية | جديد: {new_count}"
        f" | الصناعة اكتملت الآن: {done_now} | متبقي للصناعة: {left}"
        + (f" | ESPN ما وفّر الصناعة لـ {unknown}" if unknown else "")
        + (f" | تعذر جلب أحداث {failed}" if failed else "")
    )

    return out


def dump_details(details):
    return "{\n" + ",\n".join(
        f" {json.dumps(key, ensure_ascii=False)}: {json.dumps(details[key], ensure_ascii=False)}"
        for key in sorted(details)
    ) + "\n}\n"


# ---------------------------------------------------------------- التشغيل

def collect_competition(comp, year, log):
    """يجيب كل أحداث البطولة اللي تقع في موسم year (من يوليو إلى يونيو)."""
    window_start = dt.datetime(year, 7, 1, tzinfo=dt.timezone.utc)
    window_end = dt.datetime(year + 1, 7, 1, tzinfo=dt.timezone.utc)
    seasons = [year] if comp["strict"] else [year, year + 1]

    refs = []

    for season in seasons:
        url = f"{CORE}{comp['slug']}/seasons/{season}/types/1/events?limit=500"

        try:
            listing = FETCH(url)
        except UpdateError as error:
            if comp["strict"]:
                raise

            log(f"  [{comp['name']}] موسم {season}: ما توفر ({error})")
            continue

        refs += [item["$ref"] for item in listing.get("items", []) if "$ref" in item]

    log(f"{comp['name']}: {len(refs)} حدث في ESPN")

    results = map_parallel(lambda ref: safe(lambda r: parse_event(FETCH(r)), ref), refs)

    events, skipped, failed = [], 0, []

    for ref, (event, error) in zip(refs, results):
        if error is not None:
            if isinstance(error, Undecided) and not comp["strict"]:
                skipped += 1
                continue

            if comp["strict"]:
                raise error

            failed.append(ref)
            continue

        if not (window_start <= event["start"] < window_end):
            skipped += 1
            continue

        events.append(event)

    return events, failed


def run(root, dry_run=False, now=None, log=print):
    now = now or dt.datetime.now(dt.timezone.utc)
    teams = read_json(os.path.join(root, "teams.json"), [])
    names = {str(t["espnId"]): t["name"] for t in teams if t.get("espnId")}
    slugs = {str(t["espnId"]): t["id"] for t in teams if t.get("espnId")}

    if len(names) < 2:
        raise UpdateError("teams.json ما فيه espnId للفرق")

    arabic_names = {
        (normalize(k) if not k.isdigit() else k): v
        for k, v in read_json(os.path.join(root, "foreign-teams.json"), {}).items()
        if not k.startswith("_")
    }

    year = season_year(now)
    log(f"موسم {year}-{year + 1}")

    previous_text = read_text(os.path.join(root, "matches.json"))
    previous = read_json(os.path.join(root, "matches.json"), [])
    extras = read_json(os.path.join(root, "extra-matches.json"), [])

    final_before = {
        str(row["id"]): row
        for row in previous
        if row.get("id") and row.get("status") == "final"
        and row.get("homeScore") is not None
    }
    previous_by_id = {str(row["id"]): row for row in previous if row.get("id")}

    # نفحص الحالة (مؤجلة/ملغية/جارية) للمباريات اللي لعبت أو خلال 10 أيام قادمة، ونوفر الطلبات للباقي
    horizon = now + dt.timedelta(days=10)
    foreign_cache = {}
    all_rows = []
    counts = {}
    covered = set()
    final_events = []

    for comp in COMPETITIONS:
        events, failed = collect_competition(comp, year, log)
        in_window = len(events)   # كل أحداث البطولة هذا الموسم (قبل فلترة الفرق السعودية)

        # --- فحص بنية الدوري قبل أي كتابة
        if comp["strict"]:
            unknown = sorted(
                {e["home_id"] for e in events} | {e["away_id"] for e in events}
            )
            unknown = [t for t in unknown if t not in names]

            if unknown:
                raise UpdateError(
                    "فرق غير موجودة في teams.json (أضف espnId لها): " + ", ".join(unknown)
                )

            expected = len(names) * (len(names) - 1)

            if len(events) != expected:
                raise UpdateError(f"عدد مباريات الدوري {len(events)} بدل {expected}")

            if len({e["id"] for e in events}) != len(events):
                raise UpdateError("فيه مباريات دوري مكررة في ESPN")

            for team_id, name in names.items():
                home = sum(1 for e in events if e["home_id"] == team_id)
                away = sum(1 for e in events if e["away_id"] == team_id)

                if home != len(names) - 1 or away != len(names) - 1:
                    raise UpdateError(
                        f"{name}: {home} ملعبه و{away} خارج ملعبه "
                        f"(المتوقع {len(names) - 1} لكل نوع)"
                    )

        else:
            # نبقي فقط مباريات الفرق السعودية
            events = [
                e for e in events
                if e["home_id"] in names or e["away_id"] in names
            ]

        # --- الحالة والنتيجة: للمباريات اللي لعبت أو قربت (نوفر الطلبات)
        def enrich(event, comp=comp):
            key = f"{comp['slug']}:{event['id']}"
            cached = final_before.get(key)

            if cached and iso_riyadh(event["start"]) == cached["start"]:
                event["state"] = "final"
                event["scores"] = (cached["homeScore"], cached["awayScore"])
                return event

            event["state"] = "scheduled"
            event["scores"] = None

            if event["start"] <= horizon:
                event["state"] = read_status(event["status"])

                if event["state"] == "final":
                    home_score = read_score(event["home_competitor"])
                    away_score = read_score(event["away_competitor"])

                    if home_score is None or away_score is None:
                        event["state"] = "scheduled"
                    else:
                        event["scores"] = (home_score, away_score)

            return event

        events = map_parallel(enrich, events)

        # --- بناء الصفوف
        rows = []
        dropped = 0

        def side(team_id, team_obj):
            if team_id in names:
                return names[team_id], slugs[team_id], ""

            name, _ = foreign_name(team_id, team_obj, arabic_names, foreign_cache)

            return name, "", LOGO.format(team_id)

        for event in sorted(events, key=lambda e: (e["start"], e["id"])):
            if event["state"] == "dropped":
                dropped += 1
                continue

            home_name, home_slug, home_logo = side(event["home_id"], event["home_team"])
            away_name, away_slug, away_logo = side(event["away_id"], event["away_team"])

            row = {
                "id": f"{comp['slug']}:{event['id']}",
                "start": iso_riyadh(event["start"]),
                "home": home_name,
                "away": away_name,
                "homeId": home_slug,
                "awayId": away_slug,
                "competition": comp["name"],
                "homeLogo": home_logo,
                "awayLogo": away_logo,
                "venue": event["venue"],
            }

            if event["state"] == "final":
                row["homeScore"], row["awayScore"] = event["scores"]
                row["status"] = "final"
                final_events.append({
                    "key": row["id"],
                    "slug": comp["slug"],
                    "eid": event["id"],
                    "home_id": event["home_id"],
                    "away_id": event["away_id"],
                    "start": event["start"],
                })
            elif event["state"] == "live":
                row["status"] = "live"

            rows.append(row)

        # أحداث فشل جلبها مؤقتاً: نبقي نسختها القديمة بدل ما تختفي
        kept_old = 0

        for ref in failed:
            found = re.search(r"/events/(\d+)", ref)
            old = previous_by_id.get(f"{comp['slug']}:{found.group(1)}") if found else None

            if old:
                rows.append(old)
                kept_old += 1

        finals = sum(1 for r in rows if r.get("status") == "final")
        log(
            f"  → {comp['name']}: {len(rows)} مباراة | منتهية بنتيجة: {finals}"
            f" | مؤجلة/ملغية مخفية: {dropped}"
            + (f" | أحداث تعذر جلبها (أبقينا القديم): {len(failed)}" if failed else "")
        )

        if in_window:
            # ESPN عنده بيانات للبطولة هذا الموسم: هو المرجع
            covered.add(comp["name"])
            all_rows += rows
        else:
            log(f"  ! ESPN ما عنده {comp['name']} لهذا الموسم بعد: نبقي المباريات الموجودة")

        counts[comp["name"]] = len(rows)

    # --- البطولات اللي ESPN ما عنده بياناتها: نبقي القديم + اليدوي
    fallback_names = {c["name"] for c in COMPETITIONS} - covered
    seen = {(r["start"], r["home"], r["away"]) for r in all_rows}

    for row in previous + extras:
        if row["competition"] in covered:
            continue

        if row["competition"] in fallback_names or row["competition"] not in {c["name"] for c in COMPETITIONS}:
            key = (row["start"], row["home"], row["away"])

            if key not in seen:
                seen.add(key)
                all_rows.append({k: v for k, v in row.items()})

    # --- أسماء أندية ما عندنا لها اسم عربي
    unmapped = sorted({
        value[0] for value in foreign_cache.values() if value[1]
    })

    if unmapped:
        log("أندية بدون اسم عربي (تظهر بالإنجليزي، أضفها في foreign-teams.json): " + " | ".join(unmapped))

    # --- مقارنة معلوماتية مع المباريات اليدوية القديمة (ما توقف التحديث)
    seeds = [r for r in previous if not r.get("id") and r["competition"] in covered]

    if seeds:
        new_keys = {(r["start"], r["home"], r["away"]) for r in all_rows}
        same = [r for r in seeds if (r["start"], r["home"], r["away"]) in new_keys]
        log(f"مقارنة مع جدولك القديم: {len(same)} من {len(seeds)} مطابقة تماماً")

        for r in [r for r in seeds if r not in same][:8]:
            log(f"  مختلفة عن ESPN: {r['start']} {r['home']} - {r['away']} ({r['competition']})")

    all_rows.sort(key=lambda r: (r["start"], r["home"]))
    text = dump_rows(all_rows)

    # --- تفاصيل المباريات المنتهية (إضافية: أي فشل هنا ما يأثر على الجدول)
    details_path = os.path.join(root, "match-details.json")
    details_before_text = read_text(details_path)

    try:
        details_before = read_json(details_path, {})
    except ValueError:
        details_before = {}

    valid_ids = {r["id"] for r in all_rows if r.get("id")}
    covered_events = [e for e in final_events if e["key"] in valid_ids]

    try:
        details = update_details(covered_events, details_before, now, log)
    except Exception as error:  # noqa: BLE001
        log(f"تفاصيل المباريات: تعذر التحديث ({type(error).__name__}: {error}). نبقي القديم")
        details = details_before

    details = {k: v for k, v in details.items() if k in valid_ids}
    details_text = dump_details(details)
    details_changed = details_text != details_before_text
    changed = text != previous_text
    log(f"إجمالي المباريات: {len(all_rows)} | " + ("تغيّر الجدول" if changed else "ما فيه تغيير"))

    # --- ملف آخر تحديث (مرة باليوم على الأقل، أو عند أي تغيير)
    meta_path = os.path.join(root, "matches-meta.json")
    meta = read_json(meta_path, {})
    today = now.astimezone(RIYADH).strftime("%Y-%m-%d")
    write_meta = changed or details_changed or meta.get("checkedDate") != today

    if dry_run:
        log("(تشغيل تجريبي: ما انكتب شي)")
        return {"changed": changed, "rows": len(all_rows), "text": text, "details": details}

    if changed:
        write_text(os.path.join(root, "matches.json"), text)

    if details_changed:
        write_text(details_path, details_text)

    if write_meta:
        write_text(
            meta_path,
            json.dumps(
                {
                    "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "checkedDate": today,
                    "source": "ESPN",
                    "competitions": counts,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
        )

    return {"changed": changed, "rows": len(all_rows), "text": text, "details": details}


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        run(root, dry_run="--dry-run" in argv)
    except UpdateError as error:
        print("خطأ: " + str(error), file=sys.stderr)
        print("ما تغيّر شي في الملفات.", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"خطأ غير متوقع ({type(error).__name__}): {error}", file=sys.stderr)
        print("ما تغيّر شي في الملفات.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
