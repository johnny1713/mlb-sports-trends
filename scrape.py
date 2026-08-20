import urllib.request
import re
import html
import json
import math
import time
import os
import sys
from collections import Counter
from datetime import datetime

# 網頁標題列顯示的版本號。使用者看得到，有新增/改變功能時就往上調。
APP_VERSION = "2.1"

# 趨勢樣本數最低門檻：低於此場次數的趨勢視為小樣本雜訊，不參與推薦媒合
MIN_TREND_SAMPLE = 8

# Top 5 精選清單的保守命中率下限。網站說明列自己寫「<50% 僅供參考」，
# 「差一點進 Top 5」向隅區也用同一個 50 擋（前端 NEAR_MISS_MIN_SCORE），
# 但正榜以前沒有下限——實測 27 天有 5 天讓 <50% 的推薦上榜（最低 46.8%），
# 等於系統把自己標為「可忽略」的標的當成當日精選。寧可列不滿 5 筆也不推。
# 改這個值時記得同步前端的 NEAR_MISS_MIN_SCORE。
TOP_PICK_MIN_SCORE = 50

def wilson_lower_bound(wins, n, z=1.28):
    """
    命中率的 Wilson 信賴下界 (z=1.28 約為單尾 90%，排序常用值)。
    同樣命中率下樣本越大下界越高，小樣本的高命中率會被自動壓低，不需手調參數。
    """
    if not n:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / denom

def hit_lb(t):
    """
    趨勢「過盤命中率」的 Wilson 下界 (0~1)，全系統統一的排序依據。
    無法解析勝場/樣本時以 5 場 50% 保守估計。
    """
    wins, n = t.get('wins'), t.get('sample')
    if wins is None or not n:
        return wilson_lower_bound(2.5, 5)
    return wilson_lower_bound(wins, n)

def fail_lb(t):
    """
    反向命中率 (該隊「不過盤」機率) 的 Wilson 下界，用於評估弱勢對手趨勢。
    """
    wins, n = t.get('wins'), t.get('sample')
    if wins is None or not n:
        return wilson_lower_bound(2.5, 5)
    return wilson_lower_bound(n - wins, n)

def fmt_hit(t):
    """
    把趨勢的過盤紀錄格式化成 '32/45 (71%)'。

    無法解析場次時**不可退回 Units 描述**：Units 是用美國賠率算的，對台灣運彩玩家
    沒有意義，本站已全面不顯示（見 strip_roi_text）。這裡改回「紀錄不明」。
    """
    wins, n = t.get('wins'), t.get('sample')
    if wins is None or not n:
        return "紀錄不明"
    return f"{wins}/{n} ({round(wins / n * 100)}%)"

# covers 趨勢句尾的 "(+6.65 Units / 47% ROI)"。Units/ROI 是用美國賠率算的，
# 對台灣運彩玩家沒有意義，全站不顯示。2026-07-10 清掉了推薦卡片，但趨勢原文
# 漏了——它會經由 processed_trends 顯示在詳細趨勢區，也會被複製進推薦的
# strong_trend / weak_trend / team_x_trends 欄位，所以要在解析源頭就砍掉。
# 以歷史 5440 句實測：命中 4253 處、全部同一種型態、無殘留，且 "(F5)" 不受影響。
_ROI_PAREN_RE = re.compile(r'\s*\([^()]*\b(?:Units?|ROI)\b[^()]*\)')


def strip_roi_text(text):
    """移除趨勢句尾的 Units/ROI 括號，其餘原樣保留（過盤紀錄本身在句子裡）。"""
    return _ROI_PAREN_RE.sub('', text).strip()


# ------------------------------------------
# 詳細趨勢區的中譯
# ------------------------------------------
# covers 這批句子高度模板化：歷史 4224 句只有 78 種骨架，而且骨架可以拆成
# 「否定 / only / 盤口 / 樣本寫法 / 主客場」五個彼此獨立的欄位。因此這裡用一條
# 結構化 regex，而不是 Recent Form 那種 RF_PHRASES 片語表——片語表要窮舉組合，
# 這種正交結構用拆欄位的方式才不會漏。
#
# 比對不到、或盤口／範圍不在字典裡時一律回傳 None，前端原樣顯示英文原文，
# 寧可沒翻也不要翻錯或吃掉資訊。
_TREND_RE = re.compile(
    r'^The\s+(?P<team>.+?)\s+have\s+'
    r'(?P<neg>not\s+)?'
    r'(?P<only>only\s+)?'
    r'(?:hit|covered)\s+the\s+'
    r'(?P<market>.+?)\s+in\s+'
    r'(?:(?P<wins>\d+)\s+of\s+|any\s+of\s+)?'
    r'their\s+last\s+(?P<n>\d+)\s+'
    r'(?P<scope>games\s+at\s+home|away\s+games|games)\s*$'
)

# 盤口名稱用完全比對（不是子字串），沒收錄的一律不翻，避免 Team Total 被當成 Game Total
TREND_MARKET_ZH = {
    'Moneyline': '獨贏',
    'Run Line': '讓分',
    'Game Total Over': '全場大分',
    'Game Total Under': '全場小分',
    'Team Total Over': '該隊得分大分',
    'Team Total Under': '該隊得分小分',
    '1st Five Innings (F5) Moneyline': '首五局獨贏',
    '1st Five Innings (F5) Run Line': '首五局讓分',
    '1st Five Innings (F5) Team Total Over': '首五局該隊得分大分',
    '1st Five Innings (F5) Team Total Under': '首五局該隊得分小分',
}

# 量詞跟著主客場走：「最近 13 場」/「最近 35 個客場」
TREND_SCOPE_ZH = {
    'games': '場',
    'away games': '個客場',
    'games at home': '個主場',
}


def translate_trend_text(text):
    """
    把一句 covers 趨勢原文翻成中文，隊名輸出成 `@@隊名@@` 佔位符，
    由前端 renderRecentFormText() 依語言設定翻譯（與 Recent Form 共用同一套機制，
    才不會和既有的中英切換打架）。無法確定語意時回傳 None。
    """
    m = _TREND_RE.match(text.strip())
    if not m:
        return None
    market = TREND_MARKET_ZH.get(m.group('market'))
    unit = TREND_SCOPE_ZH.get(re.sub(r'\s+', ' ', m.group('scope')))
    if market is None or unit is None:
        return None

    team = m.group('team').replace('Athletics Athletics', 'Athletics')
    head = f"@@{team}@@ 最近 {int(m.group('n'))} {unit}"
    wins = m.group('wins')

    if m.group('neg'):                       # "not ... in any of their last N"
        return f"{head}一場都沒過「{market}」"
    if wins is None:                         # "in their last N" = 全數命中
        return f"{head}全部過「{market}」"
    if m.group('only'):                      # "only hit ..." = covers 標記的弱勢趨勢
        return f"{head}只有 {wins} 場過「{market}」"
    return f"{head}有 {wins} 場過「{market}」"


# ==========================================
# 網路請求模組與防擋策略
# ==========================================
def fetch_url(url, max_retries=3, backoff_factor=3.0):
    """
    使用自訂瀏覽器標頭發送 HTTP 請求，防止被 covers.com 封鎖。

    退避倍數是 3 而不是原本的 1.5。原因是「失敗得很快」的情況：
    連線逾時的話三次嘗試橫跨約 47 秒還算合理，但 covers 若直接回 429/503，
    三次請求會在 **2.5 秒內全部打完**——對方限流 30 秒、我們 2.5 秒就放棄，
    重試形同虛設。本專案一次執行要打 16 次請求（列表 1 + 單場 15），
    間隔只有 1 秒，本來就容易踩到限流。改成 3s / 9s，並優先聽 Retry-After。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7'
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read().decode('utf-8', errors='ignore')
        except Exception as e:
            if attempt >= max_retries - 1:
                print(f"  [錯誤] 在嘗試 {max_retries} 次後仍無法抓取網頁 {url}: {e}")
                return None

            # 指數從 1 起算（3s、9s）。原本是 `** attempt`，第一次退避是 factor**0 = 1 秒，
            # 等於白白浪費一次重試機會。
            sleep_time = backoff_factor ** (attempt + 1)
            # 對方明確指示等多久時以它為準（429/503 常帶 Retry-After，單位為秒）
            retry_after = getattr(e, 'headers', None) and e.headers.get('Retry-After')
            if retry_after:
                try:
                    sleep_time = max(sleep_time, min(float(retry_after), 60.0))
                except (TypeError, ValueError):
                    pass
            print(f"  [警告] 抓取 {url} 失敗 ({e})，將在 {sleep_time:.1f} 秒後進行第 {attempt+2} 次重試...")
            time.sleep(sleep_time)

# covers 的盤口表是「各家運彩公司報價」的清單，同一個市場會有多列。
# 其中會混入 alternate run line（±2.0/±2.5/±3.0），且個別莊家（實測 williamhill）
# 還會把主客兩欄的正負號寫反。詳見 CLAUDE.md 的說明。
RUN_LINE_LABEL = 'Game Line - Run Line - FT'
GAME_TOTAL_LABEL = 'Game Line - Total - FT'
STANDARD_RUN_LINE = '1.5'


def _market_segments(html_str, label, max_window=20000):
    """
    取出某個市場標籤底下的所有報價列。同市場的後續列不會重複標籤，
    因此以「下一個內容不同的 other-odds-label」作為區段終點。
    """
    segments = []
    for mark in re.finditer(re.escape(label), html_str):
        seg = html_str[mark.end():mark.end() + max_window]
        end = len(seg)
        for lab in re.finditer(r'<div class="other-odds-label">\s*(.*?)\s*</div>', seg, re.S):
            if lab.group(1).strip() != label:
                end = lab.start()
                break
        segments.append(seg[:end])
    return segments


def _odds_cells(segment):
    """回傳 [(欄位, 數值), ...]，欄位 over = 客隊欄、under = 主隊欄。"""
    cells = []
    for cell in re.finditer(
            r'class="other-(over|under)-odds"[^>]*>(.*?)'
            r'(?=class="other-(?:over|under)-odds"|<div class="other-odds-label">|$)',
            segment, re.S):
        column, body = cell.group(1), cell.group(2)
        value = re.search(r'<div class="odds upper-block">\s*<span>\s*(.*?)\s*</span>', body, re.S)
        if not value:
            continue
        text = html.unescape(value.group(1)).replace('&#x2B;', '+').strip()
        if text:
            cells.append((column, text))
    return cells


# 全場大小分盤口的合理範圍。MLB 全場總分實測落在 6.0~14.5（14.5 是落磯隊主場
# Coors Field 的高海拔球場，屬真實盤口），首五局總分則是 4.5 上下。
# 2026-07-27 出現過「買 全場小分 (小 4.5)」——那是首五局的數字混進全場市場。
MIN_GAME_TOTAL, MAX_GAME_TOTAL = 5.5, 15.0


def _valid_game_total(text):
    """數字看起來像全場總分才承認，否則當作沒抓到。"""
    try:
        return MIN_GAME_TOTAL <= float(text) <= MAX_GAME_TOTAL
    except (TypeError, ValueError):
        return False


def _parse_total_line(html_str):
    """
    全場大小分盤口。優先取頁面頂端摘要表的 Total 列——實測它與各家報價的第一列
    完全一致，但在沒有 Game Line - Total - FT 區段的頁面上仍然存在。
    取到的數字一律過 _valid_game_total()，不合理就繼續找下一個候選。
    """
    summary = re.search(
        r'<div class="other-odds-label">\s*Total\s*</div>(.*?)(?=<div class="other-odds-label">)',
        html_str, re.S)
    if summary:
        for raw in re.findall(r'<div class="odds upper-block">\s*<span>\s*(.*?)\s*</span>',
                              summary.group(1), re.S):
            text = html.unescape(raw).strip()
            if re.fullmatch(r'\d+(?:\.\d+)?', text) and _valid_game_total(text):
                return text

    for segment in _market_segments(html_str, GAME_TOTAL_LABEL):
        for _column, text in _odds_cells(segment):
            hit = re.fullmatch(r'[ou](\d+(?:\.\d+)?)', text, re.IGNORECASE)
            if hit and _valid_game_total(hit.group(1)):
                return hit.group(1)
    return None


def parse_run_lines(html_str):
    """
    解析全場讓分 (Run Line) 與大小分盤口。

    ⚠️ 只承認標準的 ±1.5，並以多數決決定誰是讓分方——不可以直接取第一列。
    covers 列的是各家報價，會混入 alternate run line，也會有莊家把正負號寫反；
    而 covers 自己的 Run Line 趨勢統計一律以標準 ±1.5 為準，取到別的數字等於
    叫使用者去下一個與趨勢不同的盤口。判斷不出來時回 None，讓畫面退回不帶數字的
    「讓分／受讓」，寧可不顯示也不要顯示錯的。
    """
    try:
        votes = Counter()
        for segment in _market_segments(html_str, RUN_LINE_LABEL):
            for column, text in _odds_cells(segment):
                if text.lstrip('+-') != STANDARD_RUN_LINE:
                    continue
                # over 欄是客隊：客隊 +1.5 代表主隊為讓分方；under 欄則相反
                home_is_favorite = (text[0] == '+') if column == 'over' else (text[0] == '-')
                votes['home' if home_is_favorite else 'away'] += 1

        spread_a = spread_b = None
        if votes['home'] != votes['away']:
            if votes['home'] > votes['away']:
                spread_a, spread_b = '+1.5', '-1.5'
            else:
                spread_a, spread_b = '-1.5', '+1.5'

        return {
            'spread_a': spread_a,
            'spread_b': spread_b,
            'total_line': _parse_total_line(html_str),
        }
    except Exception as e:
        print(f"  [警告] 提取盤口讓分值與大小值時發生錯誤: {e}")
        return None


# ==========================================
# 賽事抓取與路徑提取
# ==========================================
# 主場城市相對美東 (ET) 的時差。MLB 賽季均在夏令時間；亞利桑那不實施夏令，夏季等同太平洋時間。
# 未列出的球隊 (美東球隊) 時差為 0。
HOME_TZ_OFFSET = {
    'chc': -1, 'chw': -1, 'hou': -1, 'kc': -1, 'mil': -1, 'min': -1, 'stl': -1, 'tex': -1,  # 中部
    'col': -2,                                                                               # 山區
    'az': -3, 'ath': -3, 'laa': -3, 'lad': -3, 'sd': -3, 'sea': -3, 'sf': -3,               # 太平洋/亞利桑那
}

def parse_start_time_et(html_str):
    """
    從單場頁面的 schema.org 資料取開賽時間，轉成與賽事列表相同的 "H:MM AM/PM ET" 格式。

    賽事列表的 gamebox 在比賽開打後會把開賽時間換成比分／局數，該場的時間就變成 "None"
    （實測最早那輪 UTC 11:17 有 56% 場次抓不到）。單場頁面的 startDate 是 UTC 絕對時間、
    不受賽況影響，而且那頁本來就要抓，不必多發一次請求。
    已完賽的頁面同樣沒有 startDate，那種情況仍然回 None。
    """
    hit = re.search(r'"startDate"\s*:\s*"([^"]+)"', html_str or '')
    if not hit:
        return None
    raw = html.unescape(hit.group(1)).replace('&#x2B;', '+').strip()
    stamp = re.match(r'(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})', raw)
    if not stamp:
        return None
    month, day, year, hour, minute, second = (int(x) for x in stamp.groups())
    try:
        from datetime import timezone
        utc_dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            et_dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            # 無時區資料庫時以 UTC-4 近似，MLB 賽季均落在美東夏令時間內
            from datetime import timedelta
            et_dt = utc_dt.astimezone(timezone(timedelta(hours=-4)))
    except (ValueError, OverflowError):
        return None
    return f"{et_dt.strftime('%I:%M %p').lstrip('0')} ET"


def is_day_game(time_str, home_short=""):
    """
    以「主場當地時間」判定是否為下午場：covers 提供的開賽時間為 ET，
    依主場球隊時區換算後，當地 17:00 前開打即視為下午場。
    """
    if not time_str or time_str == "None":
        return False
    m = re.search(r'(\d{1,2}):(\d{2})\s*([AP]M)', time_str.upper())
    if not m:
        return False
    hour = int(m.group(1)) % 12
    if m.group(3) == 'PM':
        hour += 12
    local_hour = hour + HOME_TZ_OFFSET.get(home_short, 0)
    return local_hour < 17

def game_time_variants(game_time_et, home_short, date_str):
    """
    把 covers 給的美東開賽時間換算成「主場當地時間」與「台灣時間」。

    使用者在台灣，看 ET 要自己加 12 小時；而下午場的判定又是以主場當地時間為準
    （見 is_day_game），畫面上只寫 ET 兩邊都對不起來。故兩個都直接算好存進 JSON，
    前端只負責顯示——時區換算集中在 Python 一處，不要散到 JS 去。

    台灣時間一定帶日期：美東晚場對應台灣隔天凌晨，不寫日期會被誤讀成當天。
    回傳 (主場當地時間, 台灣時間)，算不出來就回 (None, None)。
    """
    stamp = re.match(r'(\d{1,2}):(\d{2})\s*([AP]M)', str(game_time_et or ''), re.IGNORECASE)
    if not stamp:
        return None, None
    hour, minute, ampm = int(stamp.group(1)), int(stamp.group(2)), stamp.group(3).upper()
    if ampm == 'PM' and hour != 12:
        hour += 12
    elif ampm == 'AM' and hour == 12:
        hour = 0

    def to_ampm(h, m):
        return '%d:%02d %s' % (h % 12 or 12, m, 'AM' if h < 12 else 'PM')

    # 主場當地時間：HOME_TZ_OFFSET 是相對 ET 的時差（亞利桑那不實施夏令時間，已含在表內）
    local_text = to_ampm((hour + HOME_TZ_OFFSET.get(home_short, 0)) % 24, minute)

    try:
        try:
            from zoneinfo import ZoneInfo
            et_tz, tw_tz = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
        except Exception:
            from datetime import timezone, timedelta
            et_tz, tw_tz = timezone(timedelta(hours=-4)), timezone(timedelta(hours=8))
        base = datetime.strptime(date_str, "%Y-%m-%d")
        tw_dt = base.replace(hour=hour, minute=minute, tzinfo=et_tz).astimezone(tw_tz)
        # 台灣時間用 24 小時制：多數比賽落在台灣半夜，00:40 比 12:40 AM 好判讀
        taiwan_text = '%d/%d %02d:%02d' % (tw_dt.month, tw_dt.day, tw_dt.hour, tw_dt.minute)
    except (ValueError, TypeError, OverflowError):
        taiwan_text = None

    return local_text, taiwan_text


def get_eastern_today():
    """
    取得美東時間今天的日期字串 (YYYY-MM-DD)。
    covers.com 預設頁在美東凌晨仍顯示前一日已完賽的賽事，必須明確帶
    selectedDate 參數才能穩定抓到當天 (美東) 尚未開打的賽事。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        # 無時區資料庫時以 UTC-4 (美東夏令時間) 近似，MLB 賽季均在夏令時間內
        from datetime import timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")

def taiwan_play_dates(date_str, matchups_data):
    """
    依實際開賽時間，換算出這批賽事在**台灣**是哪一天（或哪兩天）開打。

    美東下午/晚上的比賽對應台灣隔天凌晨到中午，所以標題只寫美東日期時，
    台灣使用者在當地隔天打開會誤以為「這是昨天、已經打完了」。
    回傳 'M/D' 或 'M/D~M/D'；無法解析時回傳 None，由呼叫端略過不顯示。
    """
    try:
        from zoneinfo import ZoneInfo
        et_tz, tw_tz = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
    except Exception:
        from datetime import timezone, timedelta
        et_tz, tw_tz = timezone(timedelta(hours=-4)), timezone(timedelta(hours=8))

    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None

    from datetime import timedelta
    dates = []
    for m in matchups_data:
        t = re.match(r'(\d{1,2}):(\d{2})\s*([AP]M)', str(m.get('game_time') or ''), re.IGNORECASE)
        if not t:
            continue
        hour, minute, ampm = int(t.group(1)), int(t.group(2)), t.group(3).upper()
        if ampm == 'PM' and hour != 12:
            hour += 12
        elif ampm == 'AM' and hour == 12:
            hour = 0
        et_dt = base.replace(hour=hour, minute=minute, tzinfo=et_tz)
        dates.append(et_dt.astimezone(tw_tz).date())

    if not dates:
        # 沒有任何可解析的開賽時間時，以美東晚場的通則（台灣隔日）近似
        fallback = (base + timedelta(days=1)).date()
        return f"{fallback.month}/{fallback.day}"

    lo, hi = min(dates), max(dates)
    if lo == hi:
        return f"{lo.month}/{lo.day}"
    return f"{lo.month}/{lo.day}~{hi.month}/{hi.day}"


def get_matchups_data(date_str):
    """
    抓取 MLB 指定日期 (美東) 賽事列表，並提取當日所有獨特的對戰頁面 ID、路徑與球隊 Logo 縮寫。
    同時解析開賽時間，並判定是否為下午場。
    """
    base_url = 'https://www.covers.com/sports/mlb/matchups'
    target_url = f"{base_url}?selectedDate={date_str}"
    print(f"[*] 正在抓取 {date_str} (美東日期) 的賽事列表...")

    html_content = fetch_url(target_url)
    if not html_content:
        print("[錯誤] 無法獲取賽事列表，請檢查網路連線。")
        return []
        
    # 尋找整個 article 標籤 block 以便從中提取開賽時間
    article_pattern = re.compile(r'(<article\s+[^>]*class="[^"]*gamebox[^"]*"[^>]*>.*?</article>)', re.IGNORECASE | re.DOTALL)
    articles = article_pattern.findall(html_content)
    
    matchups = []
    for art in articles:
        # 提取 game ID
        url_match = re.search(r'data-url=["\']?/sports/game/(\d+)["\']?', art, re.IGNORECASE)
        if not url_match:
            continue
        game_id = url_match.group(1)
        
        # 提取球隊縮寫
        away_short_match = re.search(r'data-away-team-shortname=["\']?([a-z0-9]+)["\']?', art, re.IGNORECASE)
        home_short_match = re.search(r'data-home-team-shortname=["\']?([a-z0-9]+)["\']?', art, re.IGNORECASE)
        
        away_short = away_short_match.group(1).lower() if away_short_match else ""
        home_short = home_short_match.group(1).lower() if home_short_match else ""
        
        # 提取開賽時間 (新版面格式如 "Fri, Jul 10 6:40 PM ET"，取時間部分)
        time_match = re.search(r'(\d{1,2}:\d{2}\s*[AP]M)\s*ET', art, re.IGNORECASE)
        game_time = f"{time_match.group(1)} ET" if time_match else "None"
        
        matchups.append({
            'id': game_id,
            'path': f"/sport/baseball/mlb/matchup/{game_id}",
            'away_short': away_short,
            'home_short': home_short,
            'game_time': game_time,
            'is_day_game': is_day_game(game_time, home_short)
        })
        
    print(f"[+] 成功從賽事列表解析到 {len(matchups)} 場對戰與其開賽時間。")
    return matchups

# ==========================================
# 戰績累積（history.json）
# ==========================================
# covers 只給賽前趨勢，比賽結果不在專案裡。想回答「推薦到底準不準」就得每次重抓
# 幾十天的計分板，慢、多打擾 covers、而且對方哪天改版舊資料就永遠拿不回來。
# 因此每輪把「當天的推薦」與「前一天的比分」累積進 history.json：
#   一天約 15 場 × 幾十位元組，整季不到 1 MB，跟著 index.html 一起進版控。
#
# ⚠️ 這份資料**只用來顯示**，不參與任何排序或篩選。理由和 Recent Form 相同：
# 拿三位數的樣本回頭調參數，過擬合的風險遠大於訊號。要動排序請先確認樣本厚度。
HISTORY_PATH = "history.json"
RESULT_LOOKBACK_DAYS = 7      # 往回補幾天的賽果（延賽的場次不會無限重試）
TRACK_RECENT_DAYS = 30        # 「近期」統計的天數


def load_history(path=HISTORY_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(history, path=HISTORY_PATH):
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(history, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")


def _market_key(text):
    """
    把盤口名正規化成統計用的 key。

    ⚠️ Top 5 那邊給的是 `market_type`（"受讓 1.5"、"Over (大 7.5)"），單場推薦給的是
    `market_zh`（"受讓 1.5"）。兩邊**一定要走同一個函式**——之前各自處理，Top 5 用
    `split(' ')[0]` 切出 "受讓"、單場用 "受讓 1.5"，比對永遠不成立，
    讓分盤的 top5 旗標整批沒被標到（實測 108 筆裡一筆讓分都沒有），
    而讓分又剛好是命中率最低的盤口，統計因此被系統性高估。
    """
    market = (text or '').strip()
    if market.startswith('獨贏'):
        return '獨贏'
    if market.startswith('受讓'):
        return '受讓 1.5'
    if market.startswith('讓'):
        return '讓 1.5'
    if market.startswith('Over'):
        return 'Over'
    if market.startswith('Under'):
        return 'Under'
    return None


def _pick_rows(matchups_data, top_5_ai):
    """把當天所有推薦壓成可長期保存的最小結構。"""
    def line_of(text):
        hit = re.search(r'([0-9]+(?:\.[0-9]+)?)', text or '')
        return hit.group(1) if hit else None

    # rf_agree（近期走勢是否同向）只存在於 top-ai-data，單場推薦陣列裡沒有，
    # 所以在這裡順手帶進來。三態 True/False/None 要原樣保留：None 代表「沒有明顯方向」，
    # 和「這天根本還沒有 Recent Form 資料」是兩回事，後者不會有 rf 這個 key。
    top_meta = {}
    for r in top_5_ai:
        market = r.get('market_type') or ''
        key = _market_key(market)
        if not key:
            continue
        line = line_of(market) if key in ('Over', 'Under') else None
        top_meta[(r.get('matchup_id'), key, r.get('bet_on'), line)] = (
            r.get('rf_agree') if 'rf_agree' in r else '__absent__')

    rows = []
    games = {}
    for m in matchups_data:
        gid = m['path'].rsplit('/', 1)[-1]
        games[gid] = {'away': m.get('team_a'), 'home': m.get('team_b'),
                      'day': bool(m.get('is_day_game'))}
        for r in m.get('opposing_trends', []):
            raw = r.get('market_zh') or ''
            key = _market_key(raw)
            if not key:
                continue
            meta_key = (gid, key, r.get('bet_on'), None)
            row = {'gid': gid, 'market': key, 'bet_on': r.get('bet_on'),
                   'score': r.get('score'),
                   'top5': meta_key in top_meta}
            if row['top5'] and top_meta[meta_key] != '__absent__':
                row['rf'] = top_meta[meta_key]
            # 2026-08-21 之前顯示過 alternate run line（受讓 2.0 之類，見 CLAUDE.md）。
            # 判定一律以「畫面上當時寫的那個數字」為準，並留下原文供事後稽核。
            line = line_of(raw)
            if line and line != '1.5':
                row['line'] = line
                row['market_raw'] = raw
            rows.append(row)
        for r in m.get('double_positive', []):
            # `direction` 是後來才加的欄位，2026-08-01 之前的資料沒有；
            # 退回用 market_type（"Under (小 8.5)"）判斷，否則回填時整批大小分會被跳過。
            key = _market_key(r.get('direction') or r.get('market_type'))
            if key not in ('Over', 'Under'):
                continue
            line = line_of(r.get('market_type'))
            meta_key = (gid, key, None, line)
            row = {'gid': gid, 'market': key, 'bet_on': None,
                   'line': line, 'score': r.get('score'),
                   'top5': meta_key in top_meta}
            if row['top5'] and top_meta[meta_key] != '__absent__':
                row['rf'] = top_meta[meta_key]
            rows.append(row)
    return rows, games


def record_daily_picks(history, date_str, matchups_data, top_5_ai):
    """
    記下當天的推薦。同一天會被後面幾輪覆蓋——這是刻意的：
    使用者晚上看到的是最後一輪的版本，統計就該以那一份為準。
    """
    rows, games = _pick_rows(matchups_data, top_5_ai)
    if not rows:
        return history
    day = history.setdefault(date_str, {})
    day['picks'] = rows
    day['games'] = games
    day.setdefault('results', {})
    return history


_SB_ARTICLE_RE = re.compile(r'(<article\s+[^>]*class="[^"]*gamebox[^"]*"[^>]*>.*?</article>)', re.S | re.I)
_SB_GID_RE = re.compile(r'data-url=["\']?/sports/game/(\d+)', re.I)
_SB_FINAL_RE = re.compile(r'post-game-status[^>]*>\s*Final', re.I)
_SB_SCORE_RE = re.compile(
    r'<strong class="team-score position-relative d-none d-xl-inline-block[^"]*">\s*(\d+)\s*</strong>', re.I)


def fetch_final_scores(date_str):
    """
    抓某一天的計分板，取出已完賽場次的最終比分。

    用「一天一次請求」而不是「一場一次」——同樣的資料，請求數差 15 倍。
    比分在 gamebox 內是兩個 team-score，依序為客隊、主隊。
    """
    html_text = fetch_url(f"https://www.covers.com/sports/mlb/matchups?selectedDate={date_str}")
    if not html_text:
        return None
    finals = {}
    for art in _SB_ARTICLE_RE.findall(html_text):
        gid_hit = _SB_GID_RE.search(art)
        if not gid_hit or not _SB_FINAL_RE.search(art):
            continue
        nums = _SB_SCORE_RE.findall(art)
        if len(nums) >= 2:
            finals[gid_hit.group(1)] = {'away': int(nums[0]), 'home': int(nums[1])}
    return finals


def backfill_results(history, today_str):
    """
    補上還缺結果的日子（不含今天，今天的比賽還沒打完）。

    只回看 RESULT_LOOKBACK_DAYS 天，且每輪最多補一天：延賽或取消的場次永遠不會有
    比分，沒有上限的話會每輪都重抓。已經補過的日子不會再打一次請求。
    """
    from datetime import timedelta
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return history

    for back in range(1, RESULT_LOOKBACK_DAYS + 1):
        day_str = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        day = history.get(day_str)
        if not day or not day.get('picks'):
            continue
        wanted = set(day.get('games') or {})
        have = set(day.get('results') or {})
        if not wanted - have:
            continue
        print(f"[*] 補抓 {day_str} 的比賽結果...")
        finals = fetch_final_scores(day_str)
        if finals is None:
            print(f"    [警告] {day_str} 的計分板抓取失敗，下一輪再試。")
            return history
        day.setdefault('results', {}).update(
            {gid: sc for gid, sc in finals.items() if gid in wanted})
        got = len(set(day['results']) & wanted)
        print(f"    -> {day_str}: {got}/{len(wanted)} 場已有結果")
        return history          # 一輪只補一天，控制請求量
    return history


def _judge_pick(pick, games, results):
    """
    這筆推薦有沒有過盤。回傳 True/False，無法判定（沒結果、和局）回 None。
    判定規則與網頁上的盤口說明一致：
      獨贏 = 直接獲勝；讓 1.5 = 贏 2 分以上；受讓 1.5 = 輸 1 分以內或獲勝。
    """
    gid = pick.get('gid')
    score = results.get(gid)
    if not score:
        return None
    market = pick.get('market')

    if market in ('Over', 'Under'):
        try:
            line = float(pick.get('line'))
        except (TypeError, ValueError):
            return None
        total = score['away'] + score['home']
        if total == line:
            return None                      # 整數盤口打平，不計入
        return total > line if market == 'Over' else total < line

    teams = (games.get(gid) or {})
    bet_on = pick.get('bet_on')
    if bet_on == teams.get('away'):
        diff = score['away'] - score['home']
    elif bet_on == teams.get('home'):
        diff = score['home'] - score['away']
    else:
        return None

    if market == '獨贏':
        return diff > 0

    # 讓分：以畫面上實際顯示的盤口判定（未標示則為標準的 1.5）。
    # 整數盤口會有和局，例如「受讓 2.0」輸剛好 2 分是和局而非不過，回 None 不計入。
    try:
        line = float(pick.get('line') or 1.5)
    except (TypeError, ValueError):
        line = 1.5
    if market == '讓 1.5':
        if diff == line:
            return None
        return diff > line
    if market == '受讓 1.5':
        if diff == -line:
            return None
        return diff > -line
    return None


TRACK_MARKETS = ['獨贏', '受讓 1.5', '讓 1.5', 'Over', 'Under']
TRACK_MARKET_ZH = {'獨贏': '獨贏', '受讓 1.5': '受讓 1.5', '讓 1.5': '讓 1.5',
                   'Over': '全場大分', 'Under': '全場小分'}


def compute_track_record(history, today_str, recent_days=TRACK_RECENT_DAYS):
    """
    算出 Top 5 推薦的實際過盤紀錄，供頁面底部的「過往命中率」區顯示。

    只統計 top5 為真的推薦——那才是網站真正端出來的建議；
    單場卡片上的其他推薦沒有被推薦過，混進來會稀釋掉這份紀錄的意義。
    """
    from datetime import timedelta
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        cutoff = (today - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None

    tally = {'recent': collections_counter(), 'all': collections_counter()}
    # 分組比較：下午場與近期走勢。兩者都只是切片，不改變主表的統計。
    splits = collections_counter()
    rf_days = set()
    yesterday = None
    days_with_data = set()

    for day_str in sorted(history):
        if day_str >= today_str:
            continue
        day = history[day_str] or {}
        games = day.get('games') or {}
        results = day.get('results') or {}
        if not results:
            continue
        marks = []
        for pick in day.get('picks') or []:
            if not pick.get('top5'):
                continue
            verdict = _judge_pick(pick, games, results)
            if verdict is None:
                continue
            days_with_data.add(day_str)
            marks.append(verdict)

            is_day_game_pick = bool((games.get(pick.get('gid')) or {}).get('day'))
            bucket = splits['下午場' if is_day_game_pick else '非下午場']
            bucket[1] += 1
            bucket[0] += verdict
            if 'rf' in pick:
                rf_days.add(day_str)
                # 三態一定要分開列。把「反向」和「無方向」併成「未同向」會得到相反的結論：
                # 實測反向 38.5%、無方向 65.2%，合併後 59.3% 看起來像「不同向反而比較準」，
                # 但真正的順序是 反向 < 同向 < 無方向。
                rf_label = {True: '走勢同向', False: '走勢反向'}.get(pick['rf'], '走勢無方向')
                bucket = splits[rf_label]
                bucket[1] += 1
                bucket[0] += verdict
            for scope in ('all',) + (('recent',) if day_str >= cutoff else ()):
                bucket = tally[scope]
                bucket['總計'][1] += 1
                bucket['總計'][0] += verdict
                key = pick.get('market')
                bucket[key][1] += 1
                bucket[key][0] += verdict
        if marks:
            yesterday = {'date': day_str, 'marks': marks}

    if not tally['all']['總計'][1]:
        return None

    def pack(bucket):
        out = {}
        for key, (hit, total) in bucket.items():
            if total:
                out[key] = {'hit': hit, 'total': total,
                            'rate': round(100 * hit / total, 1),
                            # 保守命中率（Wilson 下界）仍然算著但不顯示——2026-08-21
                            # 使用者反映畫面太雜要求拿掉。分母已足以提醒樣本厚度。
                            'lb': round(100 * wilson_lower_bound(hit, total), 1)}
        return out

    return {
        'recent_days': recent_days,
        'recent': pack(tally['recent']),
        'all': pack(tally['all']),
        'splits': pack(splits),
        'rf_from': min(rf_days) if rf_days else None,
        'yesterday': yesterday,
        'days': len(days_with_data),
        'markets': TRACK_MARKETS,
        'market_zh': TRACK_MARKET_ZH,
    }


def collections_counter():
    """{盤口: [命中數, 總數]}，用 defaultdict 省掉初始化判斷。"""
    from collections import defaultdict
    return defaultdict(lambda: [0, 0])


# ==========================================
# 趨勢數據解析與分類演算法
# ==========================================
def parse_matchup_details(matchup):
    """
    抓取並解析單場對戰 Picks 頁面的隊伍名稱與高/低趨勢數據。
    """
    matchup_path = matchup['path']
    url = f"https://www.covers.com{matchup_path}/picks"
    print(f"[*] 正在抓取單場對戰數據: {url} ...")
    
    html_content = fetch_url(url)
    if not html_content:
        return None
        
    # 1. 提取隊伍名稱 (從 Schema 中提取)
    team_a = "主隊"
    team_b = "客隊"
    schema_match = re.search(r'"name"\s*:\s*"([^"]+?)\s+vs\s+([^"]+?)"', html_content)
    if schema_match:
        team_a = html.unescape(schema_match.group(1).strip())
        team_b = html.unescape(schema_match.group(2).strip())
    else:
        # 備用方案：從網頁 title 中提取
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE)
        if title_match:
            title_text = html.unescape(title_match.group(1).strip())
            # 隊名可能是多個單字 (如 Red Sox、Blue Jays)，比對到常見標題結尾詞或分隔符為止
            vs_match = re.search(
                r"([A-Za-z0-9 .']+?)\s+vs\.?\s+([A-Za-z0-9 .']+?)(?=\s*(?:Predictions?|Picks?|Odds|Betting|Preview|Matchup|[|,–—-]|$))",
                title_text, re.IGNORECASE)
            if vs_match:
                team_a = vs_match.group(1).strip()
                team_b = vs_match.group(2).strip()

    # 2. 提取 High 與 Low 趨勢
    trend_pattern = re.compile(r'<h4\s+class="(High|Low)">(.*?)</h4>', re.DOTALL | re.IGNORECASE)
    trend_matches = trend_pattern.findall(html_content)
    
    raw_trends = []
    for klass, text in trend_matches:
        cleaned_text = html.unescape(text.strip())
        cleaned_text = re.sub(r'<[^>]*>', '', cleaned_text) # 去除內部 HTML 標籤
        raw_trends.append({
            'class': klass, # 'High' 代表獲利正面, 'Low' 代表虧損負面
            'text': cleaned_text
        })
        
    # 2.5 提取 Recent Form 趨勢 (與 ROI Trends 不同來源，每場固定 8 條)
    #     這些是 covers 從大量條件切片中挑出的連勝紀錄，樣本僅 4~9 場且多為全勝，
    #     屬於選擇偏誤下的產物，只作參考顯示，絕不列入評分排序。
    recent_form = []
    for raw in re.findall(r'class="single-form-trend"[^>]*>(.*?)</p>', html_content, re.DOTALL):
        cleaned = re.sub(r'<[^>]*>', '', html.unescape(raw)).strip()
        if cleaned:
            recent_form.append(cleaned)

    # 建立 Logo SVG 連結
    away_logo = f"https://img.covers.com/covers/data/svg_logos/mlb/{matchup['away_short']}.svg" if matchup['away_short'] else ""
    home_logo = f"https://img.covers.com/covers/data/svg_logos/mlb/{matchup['home_short']}.svg" if matchup['home_short'] else ""
        
    # 解析讓分、受讓與大小分總分盤口狀態
    parsed_spreads = parse_run_lines(html_content)
    team_a_spread = parsed_spreads['spread_a'] if parsed_spreads else None
    team_b_spread = parsed_spreads['spread_b'] if parsed_spreads else None
    total_line = parsed_spreads['total_line'] if parsed_spreads else None
    
    def determine_side_term(spread):
        if not spread:
            return "讓分"  # 預設為讓分
        spread = spread.strip()
        if spread.startswith('-'):
            return "讓分"
        elif spread.startswith('+') or spread.startswith('&#x2B;'):
            return "受讓"
        else:
            try:
                val = float(spread)
                if val < 0:
                    return "讓分"
                else:
                    return "受讓"
            except ValueError:
                return "讓分"
                
    team_a_side = determine_side_term(team_a_spread)
    team_b_side = determine_side_term(team_b_spread)
        
    # 賽事列表在比賽開打後拿不到開賽時間，改用單場頁面的 schema.org startDate 補上
    game_time = matchup.get('game_time', 'None')
    is_day = matchup.get('is_day_game', False)
    if not game_time or game_time == 'None':
        recovered = parse_start_time_et(html_content)
        if recovered:
            game_time = recovered
            is_day = is_day_game(game_time, matchup.get('home_short', ''))

    return {
        'path': matchup_path,
        'team_a': team_a, # 依照 Schema，通常為 Away 球隊 (客隊)
        'team_b': team_b, # 依照 Schema，通常為 Home 球隊 (主隊)
        'team_a_logo': away_logo,
        'team_b_logo': home_logo,
        'team_a_side': team_a_side,
        'team_b_side': team_b_side,
        'team_a_spread': team_a_spread,
        'team_b_spread': team_b_spread,
        'total_line': total_line,
        'trends': raw_trends,
        'recent_form': recent_form,
        'game_time': game_time,
        'is_day_game': is_day
    }

def classify_and_process_trends(matchup):
    """
    將提取的 raw trends 進行標準化分類，解析其盤口市場、方向、Units 與 ROI。
    """
    team_a = matchup['team_a']
    team_b = matchup['team_b']
    processed_trends = []
    
    # 提取數值的正規表達式
    units_pattern = re.compile(r'([+-]?\d+(?:\.\d+)?)\s+Units', re.IGNORECASE)
    roi_pattern = re.compile(r'([+-]?\d+(?:\.\d+)?)%\s+ROI', re.IGNORECASE)
    # 樣本數格式一: "in 32 of their last 45 games" -> 32 勝 / 樣本 45 場
    of_last_pattern = re.compile(r'\b(\d+)\s+of\s+(?:their|the)\s+last\s+(\d+)', re.IGNORECASE)
    # 樣本數格式二: 戰績 "7-2" -> 7 勝 / 樣本 9 場
    record_pattern = re.compile(r'\b(\d+)-(\d+)\b')

    for trend in matchup['trends']:
        text = trend['text']
        text_lower = text.lower()

        # 排除所有首五局 (F5) 的趨勢與球隊大小分 (Team Total) 趨勢，只保留全場的
        if re.search(r'\bf5\b', text_lower) or "1st five" in text_lower or "first five" in text_lower:
            continue
        if "team total" in text_lower:
            continue
            
        klass = trend['class']
        
        # 1. 判定該趨勢屬於哪支球隊 (無法確定歸屬時標記為不可信，不參與推薦媒合)
        team_confident = True
        if team_a.lower() in text_lower:
            team_match = team_a
        elif team_b.lower() in text_lower:
            team_match = team_b
        else:
            words_a = [w for w in team_a.lower().split() if len(w) > 3]
            words_b = [w for w in team_b.lower().split() if len(w) > 3]
            match_a = any(w in text_lower for w in words_a)
            match_b = any(w in text_lower for w in words_b)
            if match_a and not match_b:
                team_match = team_a
            elif match_b and not match_a:
                team_match = team_b
            else:
                mascot_a = team_a.split()[-1].lower()
                mascot_b = team_b.split()[-1].lower()
                if mascot_a in text_lower:
                    team_match = team_a
                elif mascot_b in text_lower:
                    team_match = team_b
                else:
                    team_match = team_a
                    team_confident = False

        # 2. 提取 Units、ROI 與樣本場次數
        units_match = units_pattern.search(text)
        roi_match = roi_pattern.search(text)

        units = float(units_match.group(1)) if units_match else 0.0
        roi = round(float(roi_match.group(1)), 1) if roi_match else 0

        of_last_match = of_last_pattern.search(text)
        record_match = record_pattern.search(text)
        if of_last_match:
            wins = int(of_last_match.group(1))
            sample = int(of_last_match.group(2))
            if wins > sample:
                wins, sample = None, None
        elif record_match:
            wins = int(record_match.group(1))
            sample = wins + int(record_match.group(2))
        else:
            wins, sample = None, None
        
        # 3. 判定盤口市場 (Market)。F5 與 Team Total 趨勢已在前面排除，這裡只會是全場市場
        market = "Other"
        if "game total" in text_lower:
            market = "Game Total"
        elif "moneyline" in text_lower:
            market = "Moneyline"
        elif "run line" in text_lower or "runline" in text_lower:
            market = "Run Line"
                
        # 4. 判定趨勢方向 (Direction)
        direction = "Unknown"
        if re.search(r'\bunder\b', text_lower):
            direction = "Under"
        elif re.search(r'\bover\b', text_lower):
            direction = "Over"
        elif "moneyline" in text_lower:
            if "only hit" in text_lower or units < 0 or roi < 0:
                direction = "Lose"
            else:
                direction = "Win"
        elif "run line" in text_lower or "runline" in text_lower:
            if "only covered" in text_lower or units < 0 or roi < 0:
                direction = "Fail to Cover"
            else:
                direction = "Cover"
                
        processed_trends.append({
            'team': team_match,
            'team_confident': team_confident,
            'class': klass,
            # Units/ROI 只在上面用來判方向，不外流到任何顯示欄位
            'text': strip_roi_text(text),
            # 中譯；翻不出來時為 None，前端會退回顯示英文原文
            'text_zh': translate_trend_text(strip_roi_text(text)),
            'market': market,
            'direction': direction,
            'units': units,
            'roi': roi,
            'wins': wins,
            'sample': sample
        })
        
    return processed_trends

# ==========================================
# 智能趨勢篩選演算法 (核心投注推薦邏輯)
# ==========================================
# Recent Form 條件片語的中譯表。依字串長度由長到短比對，避免短語先吃掉長語。
# 語料來自 2026-08-01 實抓的 64 條趨勢，句型高度模板化。
RF_PHRASES = [
    ('following a Quality Start in his last appearance', '上一場優質先發之後'),
    ('vs. a team with a winning record', '對戰勝率過五成的球隊'),
    ('vs. a team with a losing record', '對戰勝率不到五成的球隊'),
    ('vs. a right-handed starter', '對戰右投先發'),
    ('vs. a left-handed starter', '對戰左投先發'),
    ('vs. National League East', '對戰國聯東區'),
    ('vs. National League West', '對戰國聯西區'),
    ('vs. National League Central', '對戰國聯中區'),
    ('vs. American League East', '對戰美聯東區'),
    ('vs. American League West', '對戰美聯西區'),
    ('vs. American League Central', '對戰美聯中區'),
    ('during game 1 of a series', '系列賽首戰'),
    ('during game 2 of a series', '系列賽第 2 戰'),
    ('during game 3 of a series', '系列賽第 3 戰'),
    ('during game 4 of a series', '系列賽第 4 戰'),
    ('as a home underdog', '主場冷門時'),
    ('as a road underdog', '客場冷門時'),
    ('as a home favorite', '主場熱門時'),
    ('as a road favorite', '客場熱門時'),
    ('behind home plate', '擔任主審時'),
    ('as an underdog', '冷門時'),
    ('as a favorite', '熱門時'),
    ('interleague', '跨聯盟'),
    ('home games', '主場'),
    ('road games', '客場'),
    # 單位字（games/starts）會先被抽走，因此「home games」可能只剩下「home」
    ('home', '主場'),
    ('road', '客場'),
    ('on grass', '草地球場'),
    ('overall', '整體'),
    ('in Baltimore', '在巴爾的摩'),
    ('in Cleveland', '在克里夫蘭'),
]

# 帶數字／可自由組合的條件片語只能用 regex，寫死一定會漏。
# 實測歷史 1783 句：光是「N runs or more」「N days of rest」「WHIP greater than X」
# 「winning % of greater than .XXX」這幾類沒收，就有 15% 的走勢項目卡著英文沒翻。
# ⚠️ 這些 regex 一定要在 RF_PHRASES 之前套用，否則表尾的 'home'/'road' 單字規則
# 會先把英文子句裡的字換成中文，產生「vs. a team with a 客場 winning % of ...」那種夾雜句。
_MORE_LESS = {'more': '≥', 'less': '≤'}
_HI_LO = {'greater': '高於', 'above': '高於', 'less': '低於', 'below': '低於'}
_HOME_ROAD = {'home': '主場', 'road': '客場'}

RF_PATTERNS = [
    (re.compile(r'when their opponent (allows|scores) (\d+) runs? or (more|less) in their previous game'),
     lambda m: '對手前一場%s %s%s 時' % ('失分' if m.group(1) == 'allows' else '得分',
                                        _MORE_LESS[m.group(3)], m.group(2))),
    (re.compile(r'after (allowing|scoring) (\d+) runs? or (more|less) in their previous game'),
     lambda m: '前一場%s %s%s 之後' % ('失分' if m.group(1) == 'allowing' else '得分',
                                      _MORE_LESS[m.group(3)], m.group(2))),
    (re.compile(r'vs\. a team with a (winning|losing) (home|road) record'),
     lambda m: '對戰%s戰績%s五成的球隊' % (_HOME_ROAD[m.group(2)],
                                          '過' if m.group(1) == 'winning' else '不到')),
    (re.compile(r'vs\. a team with a (home|road) winning % of (greater|less) than (\.\d+)'),
     lambda m: '對戰%s勝率%s %s 的球隊' % (_HOME_ROAD[m.group(1)], _HI_LO[m.group(2)], m.group(3))),
    (re.compile(r'vs\. a team with a winning % (above|below) (\.\d+)'),
     lambda m: '對戰勝率%s %s 的球隊' % (_HI_LO[m.group(1)], m.group(2))),
    (re.compile(r'vs\. a starter with a WHIP (greater|less) than ([\d.]+)'),
     lambda m: '對戰 WHIP %s %s 的先發投手' % (_HI_LO[m.group(1)], m.group(2))),
    (re.compile(r'following a (home|road) trip of (\d+) or more days'),
     lambda m: '結束 ≥%s 天的%s之旅後' % (m.group(2), _HOME_ROAD[m.group(1)])),
    (re.compile(r'following a team (loss|win) in their previous game'),
     lambda m: '球隊前一場%s之後' % ('落敗' if m.group(1) == 'loss' else '獲勝')),
    (re.compile(r'with (\d+) or more days of rest'), lambda m: '休息 ≥%s 天時' % m.group(1)),
    (re.compile(r'with (\d+) days? of rest'), lambda m: '休息 %s 天時' % m.group(1)),
    (re.compile(r"with ([A-Z][A-Za-z.'-]*(?: [IVX]+)?) behind home plate"),
     lambda m: '主審 %s 時' % m.group(1)),
    (re.compile(r'following a (loss|win)\b'),
     lambda m: '%s之後' % ('落敗' if m.group(1) == 'loss' else '獲勝')),
]

# covers 句尾會用城市名指場地（in X）或對手（vs. X）。只收錄 MLB 城市，避免把人名誤譯。
RF_CITIES = {
    'Arizona': '亞利桑那', 'Atlanta': '亞特蘭大', 'Baltimore': '巴爾的摩', 'Boston': '波士頓',
    'Chicago': '芝加哥', 'Cincinnati': '辛辛那提', 'Cleveland': '克里夫蘭', 'Colorado': '科羅拉多',
    'Detroit': '底特律', 'Houston': '休士頓', 'Kansas City': '堪薩斯城', 'Los Angeles': '洛杉磯',
    'Miami': '邁阿密', 'Milwaukee': '密爾瓦基', 'Minnesota': '明尼蘇達', 'New York': '紐約',
    'Oakland': '奧克蘭', 'Philadelphia': '費城', 'Pittsburgh': '匹茲堡', 'San Diego': '聖地牙哥',
    'San Francisco': '舊金山', 'Seattle': '西雅圖', 'St. Louis': '聖路易', 'Tampa Bay': '坦帕灣',
    'Texas': '德州', 'Toronto': '多倫多', 'Washington': '華盛頓',
}
# 長名排前面，避免 'New York' 被更短的鍵切斷
RF_CITY_PATTERN = re.compile(
    r'\b(in|vs\.) (%s)\b' % '|'.join(re.escape(c) for c in sorted(RF_CITIES, key=len, reverse=True))
)
RF_PATTERNS.append(
    (RF_CITY_PATTERN,
     lambda m: ('在%s' if m.group(1) == 'in' else '對戰%s') % RF_CITIES[m.group(2)])
)

MARK = chr(0)

RECORD_RE = re.compile(r'\b(\d+)-(\d+)(?:-(\d+))?\b')


def _translate_rf_condition(text):
    """把 Recent Form 的條件片語轉成中文；未知片語原樣保留（英文），不隱藏資訊。"""
    out = text.strip()
    if not out:
        return ''
    # 譯文以 \x00 包住，標出片語邊界；否則「前一場失分 ≤2 之後」這種
    # 片語內部本來就有的空白，會被下面的頓號規則誤判成片語分隔。
    # regex 規則必須先跑，否則 RF_PHRASES 表尾的單字規則會先啃掉英文子句裡的字
    for pattern, repl in RF_PATTERNS:
        out = pattern.sub(lambda m: MARK + repl(m) + MARK, out)
    for en, zh in RF_PHRASES:
        out = out.replace(en, f'\x00{zh}\x00')
    out = re.sub(r'\x00\s+\x00', '\x00、\x00', out)
    out = out.replace('\x00', '')
    out = re.sub(r'\s{2,}', ' ', out).strip()
    return out


def parse_recent_form(raw_list, team_a=None, team_b=None):
    """
    解析 Recent Form 句型，輸出結構化資料供前端顯示。

    這些趨勢是 covers 從大量條件切片中挑出的連勝紀錄（實測 64 條有 60 條 0 敗、
    樣本僅 4~9 場），屬於選擇偏誤產物：套用 Wilson 下界會全部 >=55%，
    一旦混入排序就會淹沒真正有樣本厚度的 ROI 趨勢。故僅作參考顯示，不計分。
    """
    items = []
    for raw in raw_list:
        text = html.unescape(raw).strip().rstrip('.')
        side = None
        head_token = None  # 句子主體：Over/Under、球隊、或主客隊

        m = re.match(r'^(Over|Under)\s+is\s+([\d\-]+)\s+in\s+(.*)$', text, re.IGNORECASE)
        if m:
            side = m.group(1).capitalize()
            record, scope = m.group(2), m.group(3)
        else:
            m = re.match(r'^(.+?)\s+(?:are|is)\s+([\d\-]+)\s+in\s+(.*)$', text)
            if not m:
                items.append({'raw': text, 'zh': text, 'side': None, 'sample': 0, 'record': ''})
                continue
            head_token, record, scope = m.group(1).strip(), m.group(2), m.group(3)

        rm = RECORD_RE.search(record)
        wins, losses, pushes = (int(rm.group(1)), int(rm.group(2)), int(rm.group(3) or 0)) if rm else (0, 0, 0)
        sample = wins + losses + pushes

        # 拆出「<主體> last N <單位> <條件>」
        sm = re.match(r"^(.*?)\s*last\s+(\d+)\s*(.*)$", scope)
        if sm:
            subject_raw, n, descriptor = sm.group(1).strip(), int(sm.group(2)), sm.group(3).strip()
        else:
            subject_raw, n, descriptor = '', sample, scope

        # 單位：starts 代表投手先發，meetings 代表兩隊交手。
        # 單位字不一定在開頭（如 "interleague games"），必須全句搜尋。
        if re.search(r'\bstarts\b', descriptor):
            unit, descriptor = '次先發', re.sub(r'\bstarts\b', '', descriptor, count=1)
        elif re.search(r'\bmeetings\b', descriptor):
            unit, descriptor = '次交手', re.sub(r'\bmeetings\b', '', descriptor, count=1)
        elif re.search(r'\bgames\b', descriptor):
            unit, descriptor = '場', re.sub(r'\bgames\b', '', descriptor, count=1)
        else:
            unit = '場'
        descriptor = re.sub(r'\s{2,}', ' ', descriptor).strip()

        # 主體分類：投手/主審會用所有格（covers 省略撇號，如 "Gausmans"）
        is_person = unit == '次先發' or 'behind home plate' in descriptor
        subject_label = ''
        if subject_raw.lower() in ('the', 'their', ''):
            subject_label = '兩隊' if unit == '次交手' else ''
        elif is_person:
            name = subject_raw[:-1] if subject_raw.endswith("'") else re.sub(r's$', '', subject_raw)
            subject_label = f"{'主審' if 'behind home plate' in descriptor else '投手'} {name}"
        else:
            # 球隊簡稱保留原文，交由前端字典依語言設定翻譯
            subject_label = f'@@{subject_raw}@@'

        cond = _translate_rf_condition(descriptor)
        if head_token in ('Home team', 'Road team'):
            head_zh = '主隊' if head_token == 'Home team' else '客隊'
        elif side:
            head_zh = '大分' if side == 'Over' else '小分'
        else:
            head_zh = f'@@{head_token}@@' if head_token else ''

        scope_zh = f'{subject_label} 最近 {n} {unit}' if subject_label else f'最近 {n} {unit}'
        zh = f'{head_zh} {record}｜{scope_zh}'
        if cond:
            zh += f'（{cond}）'

        # 球隊直接勝負型（"Mets are 6-0 in ..."）才歸屬球隊，供獨贏推薦比對。
        # covers 的 Recent Form 完全沒有讓分/ATS 資料，故不會有讓分歸屬。
        win_team = None
        if side is None and head_token:
            if head_token == 'Home team':
                win_team = team_b
            elif head_token == 'Road team':
                win_team = team_a
            else:
                for full in (team_a, team_b):
                    if full and head_token.lower() in full.lower():
                        win_team = full
                        break

        items.append({
            'raw': text,
            'zh': zh,
            'side': side,          # 'Over' / 'Under' / None
            'win_team': win_team,  # 直接勝負趨勢所屬球隊（全名）；非勝負型為 None
            'record': record,
            'sample': sample,
            'losses': losses,
        })
    return items


def recent_form_lean(items):
    """
    計算該場 Recent Form 的大小分方向傾斜，僅在明顯一面倒時回傳方向。

    實測 8 場中有 5 場同時出現 Over 與 Under 的連勝（同一場兩個相反方向都「全勝」），
    足證這些切片多為雜訊，故門檻設在淨差 >= 3 才視為有傾向。
    """
    over = sum(1 for t in items if t.get('side') == 'Over')
    under = sum(1 for t in items if t.get('side') == 'Under')
    diff = over - under
    lean = None
    if diff >= 3:
        lean = 'Over'
    elif diff <= -3:
        lean = 'Under'
    return {'over': over, 'under': under, 'lean': lean}


def recent_form_agreement(matchup, rec):
    """
    這筆推薦與該場近期走勢是否同向，回傳 True/False/None(無明顯方向)。

    **只用在分數完全相同時的排序決勝**：不加分、不調整任何分數、不會讓低分排到高分前面。
    理由是 Top 5 邊界經常是平手（實測 17 天中 11 天第 5 與第 6 名相差 <1 分、2 天完全同分），
    而 Python 的 sorted 是穩定排序，同分時等於用「covers 賽事列表的順序」決定名次——那是任意的。
    以弱訊號取代任意順序，期望值不會更差。門檻與前端 recentFormStatus() 一致，改動請同步。
    """
    rf = matchup.get('recent_form') or []
    if rec.get('direction'):
        lean = (matchup.get('rf_lean') or {}).get('lean')
        return (lean == rec['direction']) if lean else None
    if rec.get('market') in ('Moneyline', 'Run Line'):
        for_count = sum(1 for t in rf if t.get('win_team') and t['win_team'] == rec.get('bet_on'))
        against_count = sum(1 for t in rf if t.get('win_team') and t['win_team'] == rec.get('bet_against'))
        if abs(for_count - against_count) < 2:
            return None
        return for_count > against_count
    return None


def dedupe_same_team_picks(recs):
    """
    同一場、同一隊的勝負盤推薦只保留分數最高的一筆，供 Top 5 清單使用。

    「買 X 獨贏」與「買 X 讓 1.5」本質是同一個看法的兩種風險版本，兩筆都擠進 Top 5
    會白白吃掉名額，也讓人誤以為有 5 個獨立標的。實測 17 天：AI Top 5 有 10 天出現
    同場重複、勝負 Top 5 有 8 天出現同隊重複（最常見的就是獨贏＋讓分成對出現）。
    單場卡片仍會完整顯示所有推薦，這裡只影響精選清單。
    """
    best = {}
    for r in recs:
        key = (r['matchup_id'], r.get('bet_on'))
        if key not in best or r['score'] > best[key]['score']:
            best[key] = r
    return list(best.values())


# 同分決勝時的近期走勢優先序：同向 > 無意見 > 反向。
# 只在分數完全相同時生效，不會讓低分排到高分前面（見 recent_form_agreement）。
_RF_RANK = {True: 0, None: 1, False: 2}


# 大小分「不需要」勝負盤那套去重／互斥檢查。單場的 double_positive 結構上最多 1 筆：
# analyze_betting_recommendations 的「全場大小分避碰」（三個分支都只 append 一筆）
# 已經在源頭決定了方向，同場不可能同時產出買大分與買小分。
#
# 2026-08-05 曾加過一支 dedupe_totals_picks 來「補防線」，2026-08-06 移除。它不只
# 永遠不會執行，而且在唯一可能被喚醒的情境下是錯的：歷史上單場出現 2 筆的 14 場
# （2026-05-29/30，F5 尚未排除時）長這樣——
#     買 全場小分 (Game Under) ＋ 買 首五局大分 (F5 Over)
# 那是**不同市場**、可以同時成立，但該函式只看 direction 不看 market，會判為互斥
# 把兩筆都丟掉；去重鍵 (matchup_id, direction) 也會把「全場小分」與「首五局小分」
# 誤當成重複。它只有在「單場只有一個市場」的前提下才正確，而那正是它不可達的原因。
# 要動大小分的取捨邏輯，請改上游那個避碰，別在下游再疊一層。


def _margin_lower_bound(rec):
    """該筆推薦要成立所需的最小分差（押注隊得分 - 對手得分）。"""
    if rec.get('market') == 'Moneyline':
        return 1        # 直接獲勝
    if rec.get('spread_side') == '讓分':
        return 2        # 讓 1.5：需贏 2 分以上
    return -1           # 受讓 1.5：輸 1 分以內即可


def picks_are_contradictory(recs):
    """
    同場的多筆勝負推薦是否真的無法同時成立。

    以分差建模：押 A 的推薦給出 M_A 的下界，押 B 的推薦等價於 M_A 的上界，
    兩者區間無交集才算矛盾（MLB 無和局，分差不為 0）。
    **只押不同隊並不等於矛盾**——「買 A 獨贏」＋「買 B 受讓 1.5」在 A 贏 1 分時同時成立。
    舊版以「押到不同隊就整場排除」判斷，實測 17 天內 14 場被排除、其中 7 場其實相容。
    """
    teams = sorted({r.get('bet_on') for r in recs})
    if len(teams) < 2:
        return False
    anchor = teams[0]
    lo, hi = -99, 99
    for r in recs:
        need = _margin_lower_bound(r)
        if r.get('bet_on') == anchor:
            lo = max(lo, need)
        else:
            hi = min(hi, -need)
    return not any(m != 0 and lo <= m <= hi for m in range(-30, 31))


def analyze_betting_recommendations(matchup, processed_trends):
    """
    根據用戶要求的兩種核心趨勢演算法進行媒合：
    1. 大小分總分 (Double Positive)：兩隊皆在大小分(Totals)中看好同一個方向(Under/Over)。
    2. 勝負/讓分盤 (Opposing Trends)：對戰雙方在同一個勝負市場（獨贏、讓分）中，一隊正數（強勢獲利）且一隊負數（弱勢虧損）。
    """
    team_a = matchup['team_a']
    team_b = matchup['team_b']

    double_positive = []
    opposing_trends = []

    # 推薦媒合只採用：(a) 隊伍歸屬可信 (b) 樣本數達門檻 (無法解析樣本者保留但由 hit_lb 保守估計壓低)
    usable_trends = [
        t for t in processed_trends
        if t.get('team_confident', True)
        and not (t.get('sample') is not None and t['sample'] < MIN_TREND_SAMPLE)
    ]

    # --- 1. 大小分總分趨勢媒合 (全場大小分) ---
    total_line = matchup.get('total_line')

    # A. 全場大小分 (Full Game Totals)
    high_under_full = [t for t in usable_trends if t['class'] == 'High' and t['direction'] == 'Under' and t['market'] == 'Game Total']
    high_over_full = [t for t in usable_trends if t['class'] == 'High' and t['direction'] == 'Over' and t['market'] == 'Game Total']
    
    a_under_full = [t for t in high_under_full if t['team'] == team_a]
    b_under_full = [t for t in high_under_full if t['team'] == team_b]
    
    under_full_rec = None
    if a_under_full and b_under_full:
        under_full_rec = {
            'direction': 'Under',  # 供前端與 Recent Form 方向比對（同向/反向標記用）
            'market_type': f"Under (小 {total_line})" if total_line else 'Under (全場小分)',
            'recommendation': f"買 全場小分 (小 {total_line})" if total_line else '買 全場小分 (Game Under)',
            'confidence': f"雙正面強勢指標：{team_a} 擁有 {len(a_under_full)} 項全場 Under 趨勢，{team_b} 擁有 {len(b_under_full)} 項全場 Under 趨勢。",
            'team_a_trends': [t['text'] for t in a_under_full],
            'team_b_trends': [t['text'] for t in b_under_full],
            'hit_detail': " + ".join(fmt_hit(t) for t in a_under_full + b_under_full),
            'score': round((sum(hit_lb(t) for t in a_under_full + b_under_full) / len(a_under_full + b_under_full)) * 100, 1)
        }
        
    a_over_full = [t for t in high_over_full if t['team'] == team_a]
    b_over_full = [t for t in high_over_full if t['team'] == team_b]
    
    over_full_rec = None
    if a_over_full and b_over_full:
        over_full_rec = {
            'direction': 'Over',  # 供前端與 Recent Form 方向比對（同向/反向標記用）
            'market_type': f"Over (大 {total_line})" if total_line else 'Over (全場大分)',
            'recommendation': f"買 全場大分 (大 {total_line})" if total_line else '買 全場大分 (Game Over)',
            'confidence': f"雙正面強勢指標：{team_a} 擁有 {len(a_over_full)} 項全場 Over 趨勢，{team_b} 擁有 {len(b_over_full)} 項全場 Over 趨勢。",
            'team_a_trends': [t['text'] for t in a_over_full],
            'team_b_trends': [t['text'] for t in b_over_full],
            'hit_detail': " + ".join(fmt_hit(t) for t in a_over_full + b_over_full),
            'score': round((sum(hit_lb(t) for t in a_over_full + b_over_full) / len(a_over_full + b_over_full)) * 100, 1)
        }

    # 全場大小分避碰 (若同場全場同時推薦大分與小分，只保留加權分數高者)
    if under_full_rec and over_full_rec:
        if under_full_rec['score'] >= over_full_rec['score']:
            double_positive.append(under_full_rec)
        else:
            double_positive.append(over_full_rec)
    elif under_full_rec:
        double_positive.append(under_full_rec)
    elif over_full_rec:
        double_positive.append(over_full_rec)
        
    # --- 2. 勝負/讓分盤趨勢媒合 (勝負盤/讓分盤) ---
    h2h_markets = ["Moneyline", "Run Line"]
    
    market_zh_map = {
        'Moneyline': '獨贏'
    }
    
    team_a_side = matchup.get('team_a_side', '讓分')
    team_b_side = matchup.get('team_b_side', '受讓')
    team_a_spread = matchup.get('team_a_spread')
    team_b_spread = matchup.get('team_b_spread')
    
    def get_spread_detail(spread, side_term):
        if not spread:
            return side_term
        abs_val = spread.replace('-', '').replace('+', '').replace('&#x2B;', '').strip()
        side_clean = "讓" if side_term == "讓分" else side_term
        return f"{side_clean} {abs_val}"
    
    for m in h2h_markets:
        a_trends = [t for t in usable_trends if t['team'] == team_a and t['market'] == m]
        b_trends = [t for t in usable_trends if t['team'] == team_b and t['market'] == m]

        # 情況 A: A 隊極強 (High), B 隊極弱 (Low)。以命中率下界挑最具代表性的趨勢
        a_strong = sorted([t for t in a_trends if t['class'] == 'High' and (t['direction'] in ['Win', 'Cover'])], key=hit_lb, reverse=True)
        b_weak = sorted([t for t in b_trends if t['class'] == 'Low' and (t['direction'] in ['Lose', 'Fail to Cover'])], key=fail_lb, reverse=True)

        if a_strong and b_weak:
            # 動態解析該隊是讓分還是受讓，並附上具體讓分值
            if m == 'Run Line':
                m_zh = get_spread_detail(team_a_spread, team_a_side)
            else:
                m_zh = market_zh_map.get(m, m)

            opposing_trends.append({
                'market': m,
                'market_zh': m_zh,
                # 讓分/受讓決定「直接勝負」對該盤口的參考強度：
                # 受讓 +1.5 直接獲勝必定過盤；讓分 -1.5 還需贏 2 分以上
                'spread_side': team_a_side if m == 'Run Line' else None,
                'bet_on': team_a,
                'bet_against': team_b,
                'recommendation': f"買 {team_a} {m_zh}",
                'confidence': f"黃金一正一反組合：{team_a} 在 {m_zh} 近期過盤 {fmt_hit(a_strong[0])}，而對手 {team_b} 僅 {fmt_hit(b_weak[0])}。",
                'strong_trend': a_strong[0]['text'],
                'weak_trend': b_weak[0]['text'],
                'hit_detail': f"{fmt_hit(a_strong[0])} vs 對手 {fmt_hit(b_weak[0])}",
                'score': round((hit_lb(a_strong[0]) + fail_lb(b_weak[0])) / 2 * 100, 1)
            })
            
        # 情況 B: B 隊極強 (High), A 隊極弱 (Low)。以命中率下界挑最具代表性的趨勢
        b_strong = sorted([t for t in b_trends if t['class'] == 'High' and (t['direction'] in ['Win', 'Cover'])], key=hit_lb, reverse=True)
        a_weak = sorted([t for t in a_trends if t['class'] == 'Low' and (t['direction'] in ['Lose', 'Fail to Cover'])], key=fail_lb, reverse=True)

        if b_strong and a_weak:
            # 動態解析該隊是讓分還是受讓，並附上具體讓分值
            if m == 'Run Line':
                m_zh = get_spread_detail(team_b_spread, team_b_side)
            else:
                m_zh = market_zh_map.get(m, m)

            opposing_trends.append({
                'market': m,
                'market_zh': m_zh,
                'spread_side': team_b_side if m == 'Run Line' else None,
                'bet_on': team_b,
                'bet_against': team_a,
                'recommendation': f"買 {team_b} {m_zh}",
                'confidence': f"黃金一正一反組合：{team_b} 在 {m_zh} 近期過盤 {fmt_hit(b_strong[0])}，而對手 {team_a} 僅 {fmt_hit(a_weak[0])}。",
                'strong_trend': b_strong[0]['text'],
                'weak_trend': a_weak[0]['text'],
                'hit_detail': f"{fmt_hit(b_strong[0])} vs 對手 {fmt_hit(a_weak[0])}",
                'score': round((hit_lb(b_strong[0]) + fail_lb(a_weak[0])) / 2 * 100, 1)
            })
            
    return double_positive, opposing_trends

# ==========================================
# 質感中文化 HTML 儀表板文件生成器
# ==========================================
# ------------------------------------------
# 頁面樣式與前端腳本
# ------------------------------------------
# 這兩塊刻意**不是** f-string。原本它們整段長在 generate_html_dashboard 的巨大
# f-string 裡，導致 CSS/JS 的每一個大括號都要寫成 {{ }}——實測 876 個加倍括號
# 只為了服務 15 個插值，而且寫錯不會報錯、是靜默壞掉（歷史上已因此出過兩次
# JS 語法錯誤）。抽出來之後這裡的大括號就照正常寫，改樣式不必再閃躲。
#
# 唯一的例外是 JS 需要 Python 的 TOP_PICK_MIN_SCORE，改用 __TOP_PICK_MIN_SCORE__
# 哨符在組頁面時替換，維持「門檻只有一個來源」。
DASHBOARD_CSS = """
        /* ==========================================
           1. 變數與基礎樣式 (Core Palette & Reset)
           ========================================== */
        :root {
            --bg-color: #080c14;
            --surface-color: rgba(22, 31, 48, 0.45);
            --surface-hover: rgba(22, 31, 48, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --text-muted: #6b7280;
            --accent-orange: #fd5000;
            --accent-orange-glow: rgba(253, 80, 0, 0.4);
            --accent-green: #00E676;
            --accent-green-glow: rgba(0, 230, 118, 0.25);
            --accent-red: #ff5252;
            --accent-red-glow: rgba(255, 82, 82, 0.25);
            --accent-blue: #00b0ff;
            --accent-gold: #ffd200;
            --accent-gold-glow: rgba(255, 210, 0, 0.25);
            --accent-purple: #a855f7;
            --accent-purple-glow: rgba(168, 85, 247, 0.25);
            --font-family: 'Inter', 'Noto Sans TC', sans-serif;
            --shadow-premium: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
            --glass-blur: blur(12px);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 10% 20%, rgba(253, 80, 0, 0.06) 0px, transparent 50%),
                radial-gradient(at 90% 80%, rgba(0, 176, 255, 0.05) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-primary);
            font-family: var(--font-family);
            min-height: 100vh;
            padding-bottom: 80px;
            overflow-x: hidden;
            -webkit-font-smoothing: antialiased;
        }

        .container {
            max-width: 1280px;
            margin: 0 auto;
            padding: 0 24px;
        }

        /* ==========================================
           2. 頂部導航欄與統計區 (Header & Statistics)
           ========================================== */
        header {
            padding: 40px 0 20px 0;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 30px;
        }

        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }

        h1 {
            font-size: 28px;
            font-weight: 800;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #ffffff 30%, #a5b4fc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        h1 span {
            font-size: 14px;
            font-weight: 600;
            color: var(--accent-orange);
            background: rgba(253, 80, 0, 0.1);
            border: 1px solid rgba(253, 80, 0, 0.3);
            padding: 4px 10px;
            border-radius: 99px;
            letter-spacing: 0;
            -webkit-text-fill-color: var(--accent-orange);
        }

        .date-badge {
            font-size: 14px;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 8px;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
        }

        /* 標示日期為美東，並補上台灣實際開打日，避免誤判賽事已結束 */
        .date-badge-et {
            font-size: 10px;
            font-weight: 700;
            color: var(--text-muted);
            border: 1px solid var(--border-color);
            border-radius: 5px;
            padding: 1px 5px;
        }

        .date-badge-tw {
            font-size: 11.5px;
            font-weight: 700;
            color: var(--accent-green);
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid rgba(0, 230, 118, 0.22);
            border-radius: 999px;
            padding: 2px 9px;
        }

        .date-badge strong {
            color: var(--text-primary);
        }

        /* 抓取失敗警告列：只有真的漏抓時才會輸出（見 generate_html_dashboard） */
        .fetch-warning {
            margin: 0 0 24px;
            padding: 14px 18px;
            border-radius: 12px;
            font-size: 13px;
            line-height: 1.8;
            color: #ffd200;
            background: rgba(255, 210, 0, 0.07);
            border: 1px solid rgba(255, 210, 0, 0.3);
        }

        .fetch-warning strong {
            color: #ffe066;
        }

        /* 統計面板網格 */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }

        .stats-card {
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: var(--shadow-premium);
        }

        .stats-card:hover {
            transform: translateY(-2px);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .stats-info h3 {
            font-size: 14px;
            color: var(--text-secondary);
            font-weight: 500;
            margin-bottom: 8px;
        }

        .stats-info p {
            font-size: 32px;
            font-weight: 800;
        }

        .stats-icon {
            width: 48px;
            height: 48px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
        }

        .stats-total .stats-icon {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
        }

        .stats-double .stats-icon {
            background: var(--accent-green-glow);
            color: var(--accent-green);
            border: 1px solid rgba(0, 230, 118, 0.2);
        }

        .stats-opposing .stats-icon {
            background: rgba(0, 176, 255, 0.1);
            color: var(--accent-blue);
            border: 1px solid rgba(0, 176, 255, 0.2);
        }

        /* ==========================================
           3. 今日雙欄 Top 5 黃金投注推薦專區 (Top 5 Columns)
           ========================================== */
        .top-section {
            margin-bottom: 45px;
        }

        .top-columns-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 28px;
        }

        @media (max-width: 992px) {
            .top-columns-grid {
                grid-template-columns: 1fr;
                gap: 30px;
            }
        }

        .top-col {
            display: flex;
            flex-direction: column;
            gap: 16px;
            /* grid 項目的 min-width 預設是 auto(=min-content)，窄螢幕下會被
               不換行的推薦文字＋走勢藥丸撐爆整欄，導致整頁橫向捲動 */
            min-width: 0;
        }

        .section-main-title {
            font-size: 18px;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--text-primary);
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 12px;
        }

        .pulse-glow {
            animation: pulse 1.5s infinite alternate;
            font-size: 20px;
        }

        .pulse-glow-green {
            animation: pulse-green 1.5s infinite alternate;
            font-size: 20px;
        }

        @keyframes pulse {
            0% { transform: scale(1); filter: drop-shadow(0 0 2px rgba(0,176,255,0.5)); }
            100% { transform: scale(1.15); filter: drop-shadow(0 0 8px rgba(0,176,255,0.9)); }
        }

        @keyframes pulse-green {
            0% { transform: scale(1); filter: drop-shadow(0 0 2px rgba(0,230,118,0.5)); }
            100% { transform: scale(1.15); filter: drop-shadow(0 0 8px rgba(0,230,118,0.9)); }
        }

        .top-rec-vertical-list {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        /* 橫向列表項目 (Sleek List Item Card) */
        .top-rec-list-item {
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 14px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            cursor: pointer;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
            position: relative;
        }

        .top-rec-list-item:hover {
            transform: translateX(6px);
            border-color: var(--hover-glow-color, var(--accent-blue));
            box-shadow: 0 4px 15px var(--hover-shadow-color, rgba(0, 176, 255, 0.15));
            background: rgba(255, 255, 255, 0.02);
        }

        .top-rec-item-left {
            display: flex;
            align-items: center;
            gap: 12px;
            overflow: hidden;
            flex-grow: 1;
        }

        .top-rec-logo-container {
            display: flex;
            align-items: center;
            position: relative;
            height: 38px;
            flex-shrink: 0;
        }

        .top-rec-logo {
            width: 34px;
            height: 34px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 4px;
        }

        .top-rec-logo-container .top-rec-logo:nth-child(2) {
            margin-left: -12px;
            position: relative;
            z-index: 2;
            background: rgba(18, 25, 38, 0.95);
        }

        .top-rec-item-info {
            display: flex;
            flex-direction: column;
            gap: 2px;
            overflow: hidden;
            min-width: 0;
        }

        .top-rec-item-match {
            font-size: 10px;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
        }

        .top-rec-item-bet {
            font-size: 14px;
            font-weight: 800;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        @media (max-width: 768px) {
            /* 走勢藥丸接在推薦文字後面，nowrap + ellipsis 會把它整個切掉，
               窄螢幕改為可換行，讓藥丸掉到下一行仍看得見 */
            .top-rec-item-bet {
                white-space: normal;
                text-overflow: clip;
            }
        }

        .top-rec-item-right {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }

        .top-rec-item-roi {
            font-size: 12px;
            font-weight: 800;
            padding: 4px 8px;
            border-radius: 6px;
        }

        /* ==========================================
           4. 篩選與控制欄 (Filters & Controls)
           ========================================== */
        .controls-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
            margin-bottom: 30px;
            border-top: 1px solid var(--border-color);
            padding-top: 30px;
        }

        /* 分類頁籤 (窄螢幕可橫向滑動，不爆版) */
        .tabs {
            display: flex;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 4px;
            border-radius: 12px;
            gap: 4px;
            max-width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
        }

        .tabs::-webkit-scrollbar {
            display: none;
        }

        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-family: var(--font-family);
            font-size: 14px;
            font-weight: 600;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 8px;
            white-space: nowrap;
            flex-shrink: 0;
        }

        .tab-btn:hover {
            color: var(--text-primary);
        }

        .tab-btn.active {
            background: var(--accent-orange);
            color: #ffffff;
            box-shadow: 0 4px 15px var(--accent-orange-glow);
        }

        .tab-count {
            font-size: 11px;
            background: rgba(255, 255, 255, 0.15);
            padding: 2px 6px;
            border-radius: 99px;
            color: #ffffff;
        }

        .tab-btn.active .tab-count {
            background: rgba(0, 0, 0, 0.2);
        }

        /* 搜尋輸入框 */
        .search-wrapper {
            position: relative;
            max-width: 320px;
            width: 100%;
        }

        .search-input {
            width: 100%;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 12px 16px 12px 40px;
            border-radius: 10px;
            color: var(--text-primary);
            font-family: var(--font-family);
            font-size: 14px;
            outline: none;
            transition: all 0.2s ease;
        }

        .search-input:focus {
            border-color: var(--accent-orange);
            background: rgba(255, 255, 255, 0.05);
            box-shadow: 0 0 15px rgba(253, 80, 0, 0.15);
        }

        .search-icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            font-size: 16px;
            pointer-events: none;
        }

        /* ==========================================
           5. 對戰卡片網格與詳情 (Match Grid & Accordion)
           ========================================== */
        .match-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 24px;
        }

        .match-card {
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: var(--shadow-premium);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .match-card:hover {
            border-color: rgba(255, 255, 255, 0.12);
            box-shadow: 0 15px 35px -5px rgba(0, 0, 0, 0.6);
        }

        /* 高亮閃爍動畫 (滾動錨點交互) */
        @keyframes borderFlash {
            0%, 100% { border-color: var(--border-color); box-shadow: var(--shadow-premium); }
            50% { border-color: var(--accent-orange); box-shadow: 0 0 35px rgba(253, 80, 0, 0.5); }
        }

        .highlight-flash {
            animation: borderFlash 2s ease;
        }

        /* 卡片頭部：隊伍與對戰名稱 */
        .match-header {
            padding: 24px 28px;
            border-bottom: 1px solid var(--border-color);
            background: rgba(255, 255, 255, 0.01);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }

        .match-meta-left {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .match-time-sub {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 6px;
            margin-left: 2px;
        }

        .teams-versus {
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }

        .team-logo {
            width: 38px;
            height: 38px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 4px;
            flex-shrink: 0;
        }

        .team-name-badge {
            font-size: 20px;
            font-weight: 800;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .vs-text {
            font-size: 13px;
            font-weight: 700;
            color: var(--text-muted);
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 4px 8px;
            border-radius: 6px;
            text-transform: uppercase;
        }

        .match-tags {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .match-tag {
            font-size: 12px;
            font-weight: 700;
            padding: 6px 12px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .match-tag.double-tag {
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid rgba(0, 230, 118, 0.3);
            color: var(--accent-green);
        }

        .match-tag.opposing-tag {
            background: rgba(0, 176, 255, 0.1);
            border: 1px solid rgba(0, 176, 255, 0.3);
            color: var(--accent-blue);
        }

        .match-tag.day-game-tag {
            background: rgba(251, 191, 36, 0.12);
            border: 1px solid rgba(251, 191, 36, 0.35);
            color: #fbbf24;
            text-shadow: 0 0 8px rgba(251, 191, 36, 0.2);
        }

        .match-tag.night-game-tag {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
        }

        .day-game-tag-ai {
            background: rgba(251, 191, 36, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(251, 191, 36, 0.3);
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
        }

        /* 下午場警示橫幅 */
        .day-game-banner {
            background: linear-gradient(90deg, rgba(251, 191, 36, 0.08) 0%, rgba(251, 191, 36, 0.02) 100%);
            border: 1px solid rgba(251, 191, 36, 0.2);
            border-radius: 12px;
            padding: 14px 18px;
            margin-bottom: 20px;
            display: flex;
            align-items: flex-start;
            gap: 12px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
        }

        .day-game-icon {
            font-size: 18px;
            color: #fbbf24;
            margin-top: 1px;
            display: inline-block;
            animation: warning-pulse 2s infinite alternate;
        }

        @keyframes warning-pulse {
            0% { transform: scale(1); filter: drop-shadow(0 0 1px rgba(251, 191, 36, 0.4)); }
            100% { transform: scale(1.1); filter: drop-shadow(0 0 5px rgba(251, 191, 36, 0.8)); }
        }

        .day-game-text {
            font-size: 13px;
            color: #e5e7eb;
            line-height: 1.6;
        }

        .day-game-text strong {
            color: #fbbf24;
        }

        /* 核心推薦區域 */
        .match-body {
            padding: 28px;
        }

        .section-title {
            font-size: 14px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .rec-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }

        @media (max-width: 768px) {
            .rec-container {
                grid-template-columns: 1fr;
            }
        }

        /* 推薦卡片樣式 */
        .rec-box {
            border-radius: 14px;
            padding: 20px;
            border: 1px dashed transparent;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.15);
        }

        /* 大小分總分推薦箱 */
        .rec-box.double-box {
            background: radial-gradient(circle at top right, rgba(0, 230, 118, 0.05), transparent 60%), rgba(255, 255, 255, 0.02);
            border-color: rgba(0, 230, 118, 0.25);
        }

        .rec-box.double-box::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--accent-green);
        }

        /* 勝負/讓分盤推薦箱 */
        .rec-box.opposing-box {
            background: radial-gradient(circle at top right, rgba(0, 176, 255, 0.05), transparent 60%), rgba(255, 255, 255, 0.02);
            border-color: rgba(0, 176, 255, 0.25);
        }

        .rec-box.opposing-box::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--accent-blue);
        }

        .rec-title-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .rec-type-badge {
            font-size: 12px;
            font-weight: 800;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
        }

        .double-box .rec-type-badge {
            background: var(--accent-green);
            color: #000000;
        }

        .opposing-box .rec-type-badge {
            background: var(--accent-blue);
            color: #000000;
        }

        .roi-badge {
            font-size: 13px;
            font-weight: 700;
            color: var(--accent-green);
            background: rgba(0, 230, 118, 0.1);
            padding: 4px 8px;
            border-radius: 6px;
        }

        .rec-headline {
            font-size: 16px;
            font-weight: 800;
            color: var(--text-primary);
        }

        .rec-desc {
            font-size: 13px;
            color: var(--text-secondary);
            line-height: 1.6;
        }

        /* 動態收合摺疊區 (Accordion Details) */
        .details-trigger {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            width: 100%;
            color: var(--text-secondary);
            font-family: var(--font-family);
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: all 0.2s ease;
        }

        .details-trigger:hover {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .details-trigger svg {
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            fill: currentColor;
        }

        .match-card.expanded .details-trigger {
            background: rgba(255, 255, 255, 0.04);
            border-bottom-left-radius: 0;
            border-bottom-right-radius: 0;
            color: var(--text-primary);
        }

        .match-card.expanded .details-trigger svg {
            transform: rotate(180deg);
            color: var(--accent-orange);
        }

        .accordion-content {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            background: rgba(255, 255, 255, 0.015);
            border-left: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
            border-bottom: 1px solid var(--border-color);
            border-bottom-left-radius: 12px;
            border-bottom-right-radius: 12px;
        }

        /* 這裡的數值只是「沒有 JS 時的保底」。實際展開高度由 syncAccordionHeight()
           以 scrollHeight 寫成 inline style，因為窄螢幕單欄排版時內容遠超過任何固定值
           （近期走勢那 8 條就會撐爆），寫死會被 overflow: hidden 直接切掉。 */
        .match-card.expanded .accordion-content {
            max-height: 4000px;
        }

        .accordion-inner {
            padding: 24px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
        }

        @media (max-width: 768px) {
            .accordion-inner {
                grid-template-columns: 1fr;
            }
        }

        /* Recent Form 參考區（不計分，樣式刻意低調於主推薦） */
        /* rf-block 是 accordion-inner 的兄弟節點，拿不到它的 24px padding，
           要自己補左右與底部，否則文字會貼齊卡片邊框（底部原本是 0）。
           分隔線刻意維持整寬，所以用 padding 而不是 margin。 */
        .rf-block {
            margin-top: 20px;
            padding: 18px 24px 24px;
            border-top: 1px dashed var(--border-color);
        }

        .rf-head {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 8px;
        }

        .rf-title {
            font-size: 14px;
            font-weight: 700;
            color: var(--text-primary);
        }

        .rf-lean-tag {
            font-size: 11px;
            font-weight: 700;
            padding: 3px 9px;
            border-radius: 999px;
            color: var(--accent-orange);
            background: rgba(255, 122, 0, 0.1);
            border: 1px solid rgba(255, 122, 0, 0.25);
        }

        .rf-lean-mixed {
            color: var(--text-muted);
            background: rgba(255, 255, 255, 0.04);
            border-color: var(--border-color);
        }

        .rf-caveat {
            font-size: 11.5px;
            line-height: 1.7;
            color: var(--text-muted);
            margin-bottom: 12px;
        }

        .rf-list {
            list-style: none;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }

        @media (max-width: 768px) {
            .rf-list {
                grid-template-columns: 1fr;
            }
        }

        .rf-item {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            font-size: 12.5px;
            line-height: 1.6;
            color: var(--text-secondary);
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 8px;
            padding: 9px 12px;
        }

        .rf-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            margin-top: 7px;
            flex-shrink: 0;
            background: var(--text-muted);
        }

        .rf-over .rf-dot { background: var(--accent-orange); }
        .rf-under .rf-dot { background: var(--accent-blue); }

        /* 差一點進 Top 5 的向隅推薦區 */
        .near-miss-section {
            margin-bottom: 40px;
            padding: 22px 24px;
            border: 1px dashed var(--border-color);
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.012);
        }

        .near-miss-caveat {
            font-size: 12px;
            line-height: 1.8;
            color: var(--text-muted);
            margin: -6px 0 16px;
        }

        .near-miss-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .near-miss-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            background: rgba(255, 255, 255, 0.015);
            cursor: pointer;
            transition: border-color 0.2s ease, background 0.2s ease;
        }

        .near-miss-item:hover, .near-miss-item:active {
            border-color: rgba(0, 230, 118, 0.25);
            background: rgba(0, 230, 118, 0.04);
        }

        .near-miss-info {
            display: flex;
            flex-direction: column;
            gap: 4px;
            min-width: 0;
        }

        .near-miss-match {
            font-size: 11.5px;
            color: var(--text-muted);
        }

        .near-miss-bet {
            font-size: 14px;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }

        .near-miss-right {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 4px;
            flex-shrink: 0;
        }

        .near-miss-gap {
            font-size: 10.5px;
            color: var(--text-muted);
        }

        /* Top 5 清單／AI 卡片上的精簡近期走勢標記 */
        .rf-pill {
            display: inline-block;
            font-size: 10.5px;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 999px;
            white-space: nowrap;
            vertical-align: middle;
        }

        .rf-pill-agree {
            color: var(--accent-green);
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid rgba(0, 230, 118, 0.22);
        }

        .rf-pill-conflict {
            color: #ffd200;
            background: rgba(255, 210, 0, 0.09);
            border: 1px solid rgba(255, 210, 0, 0.22);
        }

        /* 推薦卡上的近期走勢旁證標記 */
        .rf-flag {
            margin-top: 10px;
            font-size: 11.5px;
            font-weight: 600;
            padding: 6px 10px;
            border-radius: 8px;
            line-height: 1.6;
        }

        .rf-flag-agree {
            color: var(--accent-green);
            background: rgba(0, 230, 118, 0.08);
            border: 1px solid rgba(0, 230, 118, 0.2);
        }

        .rf-flag-conflict {
            color: #ffd200;
            background: rgba(255, 210, 0, 0.07);
            border: 1px solid rgba(255, 210, 0, 0.2);
        }

        .rf-flag-note {
            font-weight: 400;
            color: var(--text-muted);
        }

        /* 隊伍趨勢清單 */
        .team-trends-col h4 {
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 16px;
            border-left: 3px solid var(--accent-orange);
            padding-left: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .trend-list {
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .trend-item {
            background: rgba(255, 255, 255, 0.01);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 10px;
            padding: 14px 16px;
            font-size: 13px;
            line-height: 1.5;
            display: flex;
            align-items: flex-start;
            gap: 12px;
        }

        .trend-item.trend-high {
            border-left: 3px solid var(--accent-green);
        }

        .trend-item.trend-low {
            border-left: 3px solid var(--accent-red);
            color: var(--text-secondary);
        }

        .trend-class-dot {
            width: 8px;
            height: 8px;
            border-radius: 99px;
            margin-top: 5px;
            flex-shrink: 0;
        }

        .trend-high .trend-class-dot {
            background: var(--accent-green);
            box-shadow: 0 0 8px var(--accent-green-glow);
        }

        .trend-low .trend-class-dot {
            background: var(--accent-red);
            box-shadow: 0 0 8px var(--accent-red-glow);
        }

        /* ==========================================
           6. 空狀態與無數據提示
           ========================================== */
        .no-data-card {
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 60px 40px;
            text-align: center;
            box-shadow: var(--shadow-premium);
        }

        .no-data-icon {
            font-size: 48px;
            margin-bottom: 20px;
        }

        .no-data-card h2 {
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text-primary);
        }

        .no-data-card p {
            font-size: 14px;
            color: var(--text-secondary);
        }

        /* ==========================================
           AI 精選推薦專區 (AI Top 5 Section)
           ========================================== */
        .ai-section {
            margin-bottom: 45px;
            background: linear-gradient(135deg, rgba(168, 85, 247, 0.08) 0%, rgba(253, 80, 0, 0.04) 100%);
            border: 1px solid rgba(168, 85, 247, 0.2);
            border-radius: 24px;
            padding: 28px;
            box-shadow: 0 15px 35px -10px rgba(168, 85, 247, 0.15);
            backdrop-filter: var(--glass-blur);
        }

        .ai-title-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }

        .ai-main-title {
            font-size: 20px;
            font-weight: 800;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 10px;
            text-shadow: 0 0 10px rgba(168, 85, 247, 0.4);
        }

        .ai-badge-glow {
            background: linear-gradient(135deg, #a855f7 0%, #fd5000 100%);
            color: #ffffff;
            font-size: 11px;
            font-weight: 700;
            padding: 4px 12px;
            border-radius: 99px;
            box-shadow: 0 0 12px rgba(168, 85, 247, 0.5);
            letter-spacing: 0.5px;
        }

        /* 下界判讀說明列 */
        .score-legend {
            font-size: 12.5px;
            color: var(--text-secondary);
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 10px 14px;
            margin-bottom: 20px;
            line-height: 1.7;
        }

        .ai-cards-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
        }

        @media (max-width: 992px) {
            .ai-cards-grid {
                grid-template-columns: 1fr;
                gap: 20px;
            }
        }

        .ai-card {
            background: rgba(8, 12, 20, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 18px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
            cursor: pointer;
        }

        .ai-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 3px;
            background: linear-gradient(90deg, #a855f7, #fd5000);
            opacity: 0.8;
        }

        .ai-card:hover {
            transform: translateY(-5px);
            border-color: rgba(168, 85, 247, 0.4);
            box-shadow: 0 10px 25px -5px rgba(168, 85, 247, 0.15);
            background: rgba(15, 23, 42, 0.7);
        }

        .ai-card-rank {
            position: absolute;
            right: 16px;
            top: 16px;
            font-size: 28px;
            font-weight: 900;
            color: rgba(255, 255, 255, 0.03);
            font-style: italic;
            line-height: 1;
        }

        .ai-card-header {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .ai-card-tag {
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
        }

        .ai-card-tag.side-tag {
            background: rgba(0, 176, 255, 0.15);
            color: var(--accent-blue);
            border: 1px solid rgba(0, 176, 255, 0.25);
        }

        .ai-card-tag.total-tag {
            background: rgba(0, 230, 118, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(0, 230, 118, 0.25);
        }

        .ai-card-match {
            font-size: 12px;
            color: var(--text-muted);
            font-weight: 600;
        }

        .ai-card-bet {
            font-size: 16px;
            font-weight: 800;
            color: var(--text-primary);
            margin-top: 4px;
        }

        .ai-card-logos {
            display: flex;
            align-items: center;
            height: 30px;
        }

        .ai-card-logo {
            width: 28px;
            height: 28px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            padding: 3px;
        }

        .ai-card-logos .ai-card-logo:nth-child(2) {
            margin-left: -10px;
            background: rgba(8, 12, 20, 0.95);
        }

        .ai-card-rationale {
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.5;
            background: rgba(255, 255, 255, 0.02);
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid rgba(255, 255, 255, 0.03);
            flex-grow: 1;
        }

        .ai-card-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            padding-top: 10px;
            margin-top: 4px;
        }

        .ai-card-roi-label {
            font-size: 11px;
            color: var(--text-muted);
            font-weight: 600;
        }

        .ai-card-roi-val {
            font-size: 16px;
            font-weight: 800;
            color: var(--accent-gold);
            text-shadow: 0 0 8px rgba(255, 210, 0, 0.3);
        }

        /* ==========================================
           7. 頁尾
           ========================================== */
        footer {
            margin-top: 60px;
            text-align: center;
            color: var(--text-muted);
            font-size: 12px;
            letter-spacing: 0.5px;
        }

        /* 語言切換按鈕與排版 */
        .header-actions {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        /* 過往命中率（頁面底部，預設收合） */
        .track-record {
            max-width: 1600px;
            margin: 0 auto 40px;
            padding: 0 24px;
        }

        .tr-toggle {
            width: 100%;
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 16px 20px;
            background: var(--surface-color);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-primary);
            cursor: pointer;
            font-family: inherit;
            text-align: left;
            transition: border-color 0.25s ease;
        }

        .tr-toggle:hover { border-color: #a5b4fc; }

        .tr-title { font-size: 15px; font-weight: 700; }

        .tr-sub {
            font-size: 12px;
            color: var(--text-secondary);
            margin-left: auto;
        }

        .tr-caret {
            font-size: 14px;
            color: var(--text-secondary);
            transition: transform 0.25s ease;
        }

        .track-record.expanded .tr-caret { transform: rotate(180deg); }

        .tr-body {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.3s ease;
        }

        /* 無 JS 時的保底；實際高度由 toggleTrackRecord() 以 scrollHeight 寫成 inline style */
        .track-record.expanded .tr-body { max-height: 2000px; }

        .tr-yesterday {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
            padding: 18px 20px 6px;
            font-size: 13px;
        }

        .tr-yesterday-label { color: var(--text-secondary); }
        .tr-marks { display: inline-flex; gap: 4px; }
        .tr-mark { font-size: 15px; line-height: 1; }
        .tr-yesterday-score { font-weight: 700; }

        /* 表格可能比窄螢幕寬，讓它自己捲，不要撐爆頁面 */
        .tr-table-wrap {
            overflow-x: auto;
            padding: 12px 20px 0;
        }

        .tr-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            min-width: 320px;
        }

        .tr-table th, .tr-table td {
            padding: 10px 8px;
            text-align: right;
            border-bottom: 1px solid var(--border-color);
            white-space: nowrap;
        }

        .tr-table thead th {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 600;
        }

        .tr-table tbody th {
            text-align: left;
            font-weight: 600;
            color: var(--text-primary);
        }

        .tr-table .tr-total th, .tr-table .tr-total td {
            background: rgba(165, 180, 252, 0.06);
            font-weight: 700;
        }

        .tr-rate { display: block; font-weight: 700; }
        .tr-n { display: block; font-size: 11px; color: var(--text-secondary); }
        .tr-empty { color: var(--text-secondary); }

        .tr-splits {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 4px 24px;
            padding: 16px 20px 0;
        }

        .tr-split { min-width: 0; }

        .tr-split h4 {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 4px;
        }

        .tr-note {
            padding: 14px 20px 20px;
            font-size: 12px;
            line-height: 1.7;
            color: var(--text-secondary);
        }

        @media (max-width: 768px) {
            .track-record { padding: 0 16px; }
            .tr-sub { width: 100%; margin-left: 0; order: 3; }
            .tr-table th, .tr-table td { padding: 9px 6px; }
        }

        /* 搜尋清除按鈕 */
        .search-clear-btn {
            position: absolute;
            right: 12px;
            top: 50%;
            transform: translateY(-50%);
            background: transparent;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            padding: 4px;
            display: none;
            align-items: center;
            justify-content: center;
            transition: color 0.2s ease;
            border-radius: 4px;
            z-index: 5;
        }
        
        .search-clear-btn:hover {
            color: var(--text-primary);
        }

        /* 回到頂部懸浮按鈕 */
        .back-to-top {
            position: fixed;
            bottom: 30px;
            right: 30px;
            width: 46px;
            height: 46px;
            border-radius: 50%;
            background: rgba(22, 31, 48, 0.85);
            border: 1px solid rgba(165, 180, 252, 0.25);
            color: #a5b4fc;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            opacity: 0;
            visibility: hidden;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1000;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            backdrop-filter: blur(10px);
        }

        .back-to-top.show {
            opacity: 1;
            visibility: visible;
        }

        .back-to-top:hover {
            border-color: #a5b4fc;
            color: #ffffff;
            background: rgba(99, 102, 241, 0.3);
            box-shadow: 0 0 15px rgba(165, 180, 252, 0.4);
            transform: translateY(-2px);
        }

        .back-to-top:active {
            transform: translateY(1px);
        }

        /* ==========================================
           手機版優化 (Mobile Optimizations)
           ========================================== */
        /* 觸控按壓回饋 (hover 效果在觸控裝置無作用) */
        .ai-card:active,
        .stats-card:active,
        .rec-box:active,
        .top-rec-item:active {
            transform: scale(0.985);
        }

        .tab-btn:active {
            transform: scale(0.96);
        }

        @media (max-width: 768px) {
            .container {
                padding: 0 14px;
            }

            header {
                padding: 24px 0 14px 0;
                margin-bottom: 20px;
            }

            h1 {
                font-size: 22px;
            }

            /* 統計卡壓縮成一列三格，讓 AI 精選更早出現 */
            .stats-grid {
                grid-template-columns: repeat(3, 1fr);
                gap: 10px;
                margin-top: 20px;
            }

            .stats-card {
                flex-direction: column-reverse;
                align-items: center;
                text-align: center;
                gap: 6px;
                padding: 14px 8px;
            }

            .stats-icon {
                width: 32px;
                height: 32px;
                border-radius: 9px;
                font-size: 15px;
            }

            .stats-info h3 {
                font-size: 11px;
                margin-bottom: 4px;
            }

            .stats-info p {
                font-size: 22px;
            }

            /* 縮減層層疊加的留白 */
            .ai-section {
                padding: 16px;
                border-radius: 18px;
            }

            .ai-card,
            .rec-box {
                padding: 16px;
            }

            .top-section {
                margin-bottom: 32px;
            }

            /* 篩選列固定在頂端，捲動時可直接切換分類與搜尋 */
            .controls-bar {
                position: sticky;
                top: 0;
                z-index: 50;
                background: rgba(8, 12, 20, 0.92);
                backdrop-filter: blur(14px);
                -webkit-backdrop-filter: blur(14px);
                border-top: none;
                border-bottom: 1px solid var(--border-color);
                padding: 12px 14px;
                margin: 0 -14px 20px -14px;
                gap: 10px;
            }

            .tabs {
                width: 100%;
            }

            .tab-btn {
                padding: 9px 14px;
                font-size: 13px;
            }

            .search-wrapper {
                max-width: 100%;
            }
        }
    """

DASHBOARD_JS = """        // ==========================================
        // MLB 隊伍名稱中英文對照字典
        // ==========================================
        const teamTranslations = {
            "Arizona Diamondbacks": "亞利桑那響尾蛇",
            "Atlanta Braves": "亞特蘭大勇士",
            "Baltimore Orioles": "巴爾的摩金鶯",
            "Boston Red Sox": "波士頓紅襪",
            "Chicago Cubs": "芝加哥小熊",
            "Chicago White Sox": "芝加哥白襪",
            "Cincinnati Reds": "辛辛那提紅人",
            "Cleveland Guardians": "克里夫蘭守護者",
            "Colorado Rockies": "科羅拉多落磯",
            "Detroit Tigers": "底特律老虎",
            "Houston Astros": "休士頓太空人",
            "Kansas City Royals": "堪薩斯皇家",
            "Los Angeles Angels": "洛杉磯天使",
            "Los Angeles Dodgers": "洛杉磯道奇",
            "Miami Marlins": "邁阿密馬林魚",
            "Milwaukee Brewers": "密爾瓦基釀酒人",
            "Minnesota Twins": "明尼蘇達雙城",
            "New York Mets": "紐約大都會",
            "New York Yankees": "紐約洋基",
            "Athletics Athletics": "奧克蘭運動家",
            "Athletics": "奧克蘭運動家",
            "Oakland Athletics": "奧克蘭運動家",
            "Philadelphia Phillies": "費城費城人",
            "Pittsburgh Pirates": "匹茲堡海盜",
            "San Diego Padres": "聖地牙哥教士",
            "San Francisco Giants": "舊金山巨人",
            "Seattle Mariners": "西雅圖水手",
            "St. Louis Cardinals": "聖路易紅雀",
            "Tampa Bay Rays": "坦帕灣光芒",
            "Texas Rangers": "德州遊騎兵",
            "Toronto Blue Jays": "多倫多藍鳥",
            "Washington Nationals": "華盛頓國民",
            "Diamondbacks": "亞利桑那響尾蛇",
            "Braves": "亞特蘭大勇士",
            "Orioles": "巴爾的摩金鶯",
            "Red Sox": "波士頓紅襪",
            "Cubs": "芝加哥小熊",
            "White Sox": "芝加哥白襪",
            "Reds": "辛辛那提紅人",
            "Guardians": "克里夫蘭守護者",
            "Rockies": "科羅拉多落磯",
            "Tigers": "底特律老虎",
            "Astros": "休士頓太空人",
            "Royals": "堪薩斯皇家",
            "Angels": "洛杉磯天使",
            "Dodgers": "洛杉磯道奇",
            "Marlins": "邁阿密馬林魚",
            "Brewers": "密爾瓦基釀酒人",
            "Twins": "明尼蘇達雙城",
            "Mets": "紐約大都會",
            "Yankees": "紐約洋基",
            "Phillies": "費城費城人",
            "Pirates": "匹茲堡海盜",
            "Padres": "聖地牙哥教士",
            "Giants": "舊金山巨人",
            "Mariners": "西雅圖水手",
            "Cardinals": "聖路易紅雀",
            "Rays": "坦帕灣光芒",
            "Rangers": "德州遊騎兵",
            "Blue Jays": "多倫多藍鳥",
            "Nationals": "華盛頓國民"
        };

        // 全站固定繁體中文。中英切換鈕已於 2026-08-21 移除（使用者從來沒用過），
        // 但隊名字典仍然要留著——畫面上的隊名、推薦文案、趨勢原文都靠它翻。
        function translateText(text) {
            if (!text) return text;
            let translated = text;
            const sortedKeys = Object.keys(teamTranslations).sort((a, b) => b.length - a.length);
            for (const key of sortedKeys) {
                const regex = new RegExp(key, 'g');
                translated = translated.replace(regex, teamTranslations[key]);
            }
            return translated;
        }

        let allMatchups = [];
        let topSides = [];
        let topTotals = [];
        let topAi = [];
        let currentTab = 'all';
        let searchQuery = '';

        window.addEventListener('DOMContentLoaded', () => {
            const rawData = document.getElementById('matchups-data').textContent;
            const rawSides = document.getElementById('top-sides-data').textContent;
            const rawTotals = document.getElementById('top-totals-data').textContent;
            const rawAi = document.getElementById('top-ai-data').textContent;
            try {
                allMatchups = JSON.parse(rawData);
                topSides = JSON.parse(rawSides);
                topTotals = JSON.parse(rawTotals);
                topAi = JSON.parse(rawAi);
                
                renderAiTop5();
                renderTopLists();
                renderNearMisses();
                renderMatchups();
            } catch(e) {
                console.error("解析 JSON 數據出錯:", e);
                document.getElementById('matchups-container').innerHTML = `
                    <div class="no-data-card">
                        <div class="no-data-icon">⚠️</div>
                        <h2>數據加載錯誤</h2>
                        <p>無法讀取嵌入的 JSON 對戰數據。</p>
                    </div>
                `;
            }
        });

        // 過往命中率的展開／收合。高度用 scrollHeight 實測寫成 inline style——
        // 和摺疊卡片同一個理由：寫死的 max-height 在窄螢幕會把內容切掉。
        function toggleTrackRecord() {
            const section = document.getElementById('track-record');
            const body = document.getElementById('tr-body');
            if (!section || !body) return;
            const open = section.classList.toggle('expanded');
            body.style.maxHeight = open ? body.scrollHeight + 'px' : '0px';
            const btn = section.querySelector('.tr-toggle');
            if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        // 開賽時間：主場當地時間 + 台灣時間（都由 Python 算好存在 JSON 裡）。
        // 舊的 index.html 沒有這兩個欄位，退回顯示原本的美東時間，--replay 才不會壞。
        function formatGameTime(m) {
            const parts = [];
            if (m.local_time) parts.push('當地 ' + m.local_time);
            if (m.taiwan_time) parts.push('台灣 ' + m.taiwan_time);
            if (parts.length) return parts.join(' ｜ ');
            if (m.game_time && m.game_time !== 'None') return m.game_time;
            return '開賽時間未提供';
        }

        // Recent Form 句子裡的球隊簡稱以 @@Rays@@ 形式標記，翻成中文隊名
        function renderRecentFormText(zh) {
            return zh.replace(/@@([^@]+)@@/g, (_, name) => translateText(name));
        }

        // 詳細趨勢區的單行文字。優先用 Python 譯好的 text_zh（隊名是 @@佔位符@@，
        // 與 Recent Form 共用同一套替換機制）；翻不出來時退回 covers 原文。
        function renderTrendText(t) {
            if (t.text_zh) {
                return renderRecentFormText(t.text_zh);
            }
            return (t.text || '').replace(/Athletics Athletics/g, 'Athletics');
        }

        // 勝負盤（獨贏／讓分）的近期走勢評估——單場卡片與 Top 5 共用的唯一判斷來源。
        //
        // covers 的 Recent Form 只有「直接勝負」紀錄，沒有讓分資料，但兩者關聯依盤口而異：
        //   受讓 +1.5：直接獲勝**必定**過盤（獲勝是過盤的子集），因此直接勝負是有效佐證。
        //   讓分 -1.5：直接獲勝只是必要條件，還得贏 2 分以上，故標為「較弱」。
        // 實測 30 隊的獨贏命中率與讓分過盤率相關係數僅 0.407，不足以等同看待。
        function recentFormSideEval(m, rec) {
            if (!m || !rec || rec.market !== 'Moneyline' && rec.market !== 'Run Line') return null;
            const rf = m.recent_form || [];
            const forCount = rf.filter(t => t.win_team && t.win_team === rec.bet_on).length;
            const againstCount = rf.filter(t => t.win_team && t.win_team === rec.bet_against).length;
            const diff = forCount - againstCount;
            if (Math.abs(diff) < 2) return null;
            return {
                status: diff > 0 ? 'agree' : 'conflict',
                forCount, againstCount,
                weak: rec.market === 'Run Line' && rec.spread_side === '讓分'
            };
        }

        // Top 5 清單用的精簡版標記。規則與單場卡片完全一致，差別只在版面。
        function recentFormStatus(rec) {
            const m = (allMatchups || []).find(x => x.path.split('/').pop() === String(rec.matchup_id));
            if (!m) return null;
            if (rec.type === 'double') {
                const lean = (m.rf_lean || {}).lean;
                if (!lean || !rec.direction) return null;
                return { status: lean === rec.direction ? 'agree' : 'conflict', weak: false };
            }
            if (rec.type === 'opposing') {
                const ev = recentFormSideEval(m, rec);
                return ev ? { status: ev.status, weak: ev.weak } : null;
            }
            return null;
        }

        function recentFormPill(rec) {
            const ev = recentFormStatus(rec);
            if (!ev) return '';
            const label = (ev.status === 'agree' ? '✅ 走勢同向' : '⚠️ 走勢反向') + (ev.weak ? '(弱)' : '');
            return `<span class="rf-pill rf-pill-${ev.status}">${label}</span>`;
        }

        // 勝負盤推薦的完整旁證標記，資料來源一律是「直接勝負」紀錄，文案依盤口說清楚適用性。
        function recentFormSideFlag(m, rec) {
            const ev = recentFormSideEval(m, rec);
            if (!ev) return '';
            const agree = ev.status === 'agree';
            const focus = agree ? rec.bet_on : rec.bet_against;
            let applicability = '';
            if (rec.market === 'Run Line') {
                applicability = ev.weak
                    ? '——但本推薦為讓分 1.5，需贏 2 分以上才過盤，直接勝負僅供參考'
                    : '——本推薦為受讓 1.5，直接獲勝即必定過盤';
            }
            return `
                <div class="rf-flag ${agree ? 'rf-flag-agree' : 'rf-flag-conflict'}">
                    ${agree ? '✅ 近期走勢同向' : '⚠️ 近期走勢反向'}${ev.weak ? '（較弱）' : ''}：近期直接勝負的連勝紀錄集中在
                    ${translateText(focus)}（${ev.forCount} 比 ${ev.againstCount}）${applicability}
                    <span class="rf-flag-note">（僅旁證，未計入分數）</span>
                </div>
            `;
        }

        // 推薦方向 vs 近期走勢的旁證標記。只在近期走勢明顯一面倒時顯示，
        // 且刻意不影響分數與排序——這些趨勢樣本太小且經過篩選，只能當提醒。
        function recentFormFlag(m, direction) {
            const lean = (m.rf_lean || {}).lean;
            if (!direction || !lean) return '';
            const agree = lean === direction;
            const leanZh = lean === 'Over' ? '大分' : '小分';
            return `
                <div class="rf-flag ${agree ? 'rf-flag-agree' : 'rf-flag-conflict'}">
                    ${agree ? '✅ 近期走勢同向' : '⚠️ 近期走勢反向'}：該場近期連勝紀錄偏向${leanZh}
                    <span class="rf-flag-note">（僅旁證，未計入分數）</span>
                </div>
            `;
        }

        // 全場趨勢皆為 0 代表 covers 尚未發佈（並非篩選後沒有結果），兩者要分開提示
        function trendsPending() {
            if (!allMatchups || allMatchups.length === 0) return false;
            return allMatchups.reduce((sum, m) => sum + (m.processed_trends || []).length, 0) === 0;
        }

        // 依情境回傳空狀態文案：無賽事 / covers 未發佈 / 有趨勢但無合格組合
        function emptyStateText(marketLabel) {
            if (!allMatchups || allMatchups.length === 0) {
                return '今日無賽事資料。';
            }
            if (trendsPending()) {
                return '⏳ covers.com 尚未發佈今日趨勢（通常美東上午發佈完畢），稍後的自動更新會補上，請晚點再看。';
            }
            return `今日暫無符合篩選標準的「${marketLabel}」推薦組合。`;
        }

        // 依「保守命中率下界」強弱回傳顏色：綠 >=55 強 / 黃 50~55 普通 / 灰 <50 弱
        function scoreColor(s) {
            if (s >= 55) return 'var(--accent-green)';
            if (s >= 50) return '#ffd200';
            return 'var(--text-muted)';
        }

        function scoreBadgeStyle(s) {
            if (s >= 55) return 'color: var(--accent-green); background: rgba(0, 230, 118, 0.12); border: 1px solid rgba(0, 230, 118, 0.25);';
            if (s >= 50) return 'color: #ffd200; background: rgba(255, 210, 0, 0.1); border: 1px solid rgba(255, 210, 0, 0.25);';
            return 'color: var(--text-muted); background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border-color);';
        }

        function renderAiTop5() {
            const section = document.getElementById('ai-top5-section');
            if (!section) return;
            
            if (topAi.length === 0) {
                // 整區隱藏會讓人以為網站壞了（使用者實際反映過），所以三種空狀態都要講清楚：
                // covers 未發佈 / 今日無賽事 / 有趨勢但沒有推薦達到 50% 門檻
                const pending = trendsPending();
                const msg = pending
                    ? `⏳ <strong style="color: #ffd200;">covers.com 尚未發佈今日趨勢</strong>，因此暫時沒有推薦。
                       趨勢通常在美東上午發佈完畢，之後的自動更新會補上——請稍後再重新整理。`
                    : `😴 <strong style="color: #ffd200;">今日沒有達標的推薦</strong>：所有組合的保守命中率都低於
                       ${TOP_PICK_MIN_SCORE}%，依本站標準屬於「僅供參考」等級，因此不列入精選。
                       <strong>這是正常結果，不是資料出錯</strong>——冷門日寧可不推。`;
                section.innerHTML = `
                    <div class="ai-title-row">
                        <h2 class="ai-main-title">🤖 今日 AI 智慧精選 Top 5 黃金推薦</h2>
                    </div>
                    <div class="score-legend" style="border-color: rgba(255, 210, 0, 0.3);">
                        ${msg}
                    </div>
                `;
                section.style.display = '';
                return;
            }
            
            let cardsHtml = '';
            topAi.forEach((rec, idx) => {
                const rankNum = idx + 1;
                const tagClass = rec.type === 'opposing' ? 'side-tag' : 'total-tag';
                const tagText = rec.type === 'opposing' ? '🎯 勝負/讓分' : '🔥 大小總分';
                const roiLabel = '保守命中率';
                
                let logosHtml = '';
                if (rec.logo_b) {
                    logosHtml = `
                        <img src="${rec.logo_a}" class="ai-card-logo" onerror="this.style.display='none'" />
                        <img src="${rec.logo_b}" class="ai-card-logo" onerror="this.style.display='none'" />
                    `;
                } else {
                    logosHtml = `
                        <img src="${rec.logo_a}" class="ai-card-logo" onerror="this.style.display='none'" />
                    `;
                }
                
                const dayGameBadge = rec.is_day_game 
                    ? `<span class="ai-card-tag day-game-tag-ai">⚠️ 下午場</span>` 
                    : '';
                
                cardsHtml += `
                    <div class="ai-card" onclick="scrollToMatch('match-card-${rec.matchup_id}')">
                        <div class="ai-card-rank">#0${rankNum}</div>
                        <div class="ai-card-header">
                            <span class="ai-card-tag ${tagClass}">${tagText}</span>
                            ${dayGameBadge}
                            ${recentFormPill(rec)}
                            <span class="ai-card-match">${translateText(rec.matchup_name)}</span>
                        </div>
                        <div>
                            <div class="ai-card-bet">${translateText(rec.recommendation)}</div>
                        </div>
                        <div class="ai-card-logos">
                            ${logosHtml}
                        </div>
                        <div class="ai-card-rationale">
                            ${translateText(rec.rationale)}
                        </div>
                        <div class="ai-card-footer">
                            <span class="ai-card-roi-label">${roiLabel}</span>
                            <span class="ai-card-roi-val" style="color: ${scoreColor(rec.score)}">${rec.score}%</span>
                        </div>
                    </div>
                `;
            });
            
            section.innerHTML = `
                <div class="ai-title-row">
                    <h2 class="ai-main-title">
                        🤖 今日 AI 智慧精選 Top 5 黃金推薦
                    </h2>
                    <span class="ai-badge-glow">AI OPTIMIZED</span>
                </div>
                <div class="score-legend">
                    📖 保守命中率怎麼看：<span style="color: var(--accent-green); font-weight: 700;">≥55% 強訊號</span> ·
                    <span style="color: #ffd200; font-weight: 700;">50~55% 普通</span> ·
                    <span style="color: var(--text-muted); font-weight: 700;">&lt;50% 僅供參考</span>
                    （過盤率已依樣本數扣除運氣水分，小樣本會被自動壓低）
                </div>
                <div class="ai-cards-grid">
                    ${cardsHtml}
                </div>
            `;
        }

        // 「差一點」的判定門檻。近期走勢刻意不影響排序，所以落榜的推薦本來就代表
        // 命中率證據較弱——只有在分數差小到沒有實質意義時，走勢同向才值得拿來補救。
        const NEAR_MISS_MARGIN = 3.0;   // 與該類 Top 5 最低分的差距上限
        // 由 Python 的 TOP_PICK_MIN_SCORE 帶入，讓正榜與向隅區共用同一個門檻。
        // 以前兩邊各寫各的：向隅區擋掉 <50%，正榜卻放行，導致 49 分的推薦
        // 進不了向隅區反而進得了 Top 5。改門檻只要改 Python 那一個常數。
        const TOP_PICK_MIN_SCORE = __TOP_PICK_MIN_SCORE__;
        const NEAR_MISS_MIN_SCORE = TOP_PICK_MIN_SCORE;

        function collectNearMisses() {
            if (!allMatchups || allMatchups.length === 0) return [];
            const inTop = new Set(
                [...topSides, ...topTotals].map(r => `${r.matchup_id}|${r.recommendation}`)
            );
            // Top 5 已列出同場同隊的推薦時，這裡不再列出它的另一種盤口版本
            // （買 X 獨贏與買 X 讓 1.5 是同一個看法，重複列出只會佔版面）
            const shownTeams = new Set(
                [...topSides, ...topTotals]
                    .filter(r => r.bet_on)
                    .map(r => `${r.matchup_id}|${r.bet_on}`)
            );
            const cutSides = topSides.length ? Math.min(...topSides.map(r => r.score)) : null;
            const cutTotals = topTotals.length ? Math.min(...topTotals.map(r => r.score)) : null;
            const out = [];
            allMatchups.forEach(m => {
                const mid = m.path.split('/').pop();
                const consider = (rec, type, cutoff, marketLabel) => {
                    if (cutoff === null) return;
                    if (inTop.has(`${mid}|${rec.recommendation}`)) return;
                    if (rec.bet_on && shownTeams.has(`${mid}|${rec.bet_on}`)) return;
                    if (rec.score < NEAR_MISS_MIN_SCORE) return;
                    if (cutoff - rec.score > NEAR_MISS_MARGIN) return;
                    const ev = recentFormStatus(Object.assign({}, rec, { matchup_id: mid, type }));
                    if (!ev || ev.status !== 'agree') return;
                    out.push({
                        matchup_id: mid,
                        matchup_name: `${m.team_a} vs ${m.team_b}`,
                        market_label: marketLabel,
                        bet_on: rec.bet_on || null,
                        recommendation: rec.recommendation,
                        score: rec.score,
                        gap: Math.round((cutoff - rec.score) * 10) / 10,
                        weak: ev.weak
                    });
                };
                (m.opposing_trends || []).forEach(r => consider(r, 'opposing', cutSides, r.market_zh));
                (m.double_positive || []).forEach(r => consider(r, 'double', cutTotals, r.market_type));
            });
            // 與 Top 5 相同的去重原則：同場同隊只留分數最高的一筆
            // （否則「買 X 受讓 1.5」與「買 X 獨贏」會在這裡並列，等於換個地方重複）
            out.sort((a, b) => b.score - a.score);
            const kept = [];
            const seenTeam = new Set();
            out.forEach(it => {
                if (it.bet_on) {
                    const key = `${it.matchup_id}|${it.bet_on}`;
                    if (seenTeam.has(key)) return;
                    seenTeam.add(key);
                }
                kept.push(it);
            });
            return kept.slice(0, 5);
        }

        function renderNearMisses() {
            const section = document.getElementById('near-miss-section');
            if (!section) return;
            const items = collectNearMisses();
            if (items.length === 0) {
                section.style.display = 'none';
                section.innerHTML = '';
                return;
            }
            const rows = items.map(it => `
                <div class="near-miss-item" onclick="scrollToMatch('match-card-${it.matchup_id}')">
                    <div class="near-miss-info">
                        <span class="near-miss-match">${translateText(it.matchup_name)} • ${it.market_label}</span>
                        <span class="near-miss-bet">
                            ${translateText(it.recommendation)}
                            <span class="rf-pill rf-pill-agree">✅ 走勢同向${it.weak ? '(弱)' : ''}</span>
                        </span>
                    </div>
                    <div class="near-miss-right">
                        <span class="top-rec-item-roi" style="${scoreBadgeStyle(it.score)}">保守 ${it.score}%</span>
                        <span class="near-miss-gap">差 ${it.gap} 分</span>
                    </div>
                </div>
            `).join('');
            section.innerHTML = `
                <h2 class="section-main-title">👀 差一點進 Top 5，但近期走勢同向</h2>
                <p class="near-miss-caveat">
                    以下推薦的保守命中率只比 Top 5 門檻低 ${NEAR_MISS_MARGIN} 分以內（等於實質平手），
                    且近期走勢方向一致，因此一併列出供你留意。
                    <strong>排序仍只看保守命中率</strong>，近期走勢不加分；分數低於 ${NEAR_MISS_MIN_SCORE}% 的一律不列。
                </p>
                <div class="near-miss-list">${rows}</div>
            `;
            section.style.display = '';
        }

        function renderTopLists() {
            const sidesContainer = document.getElementById('top-sides-container');
            sidesContainer.innerHTML = '';
            
            if (topSides.length === 0) {
                sidesContainer.innerHTML = `<div class="no-data-card" style="padding: 24px;"><p style="font-size: 13px; color: var(--text-muted); font-style: italic;">${emptyStateText('勝負/讓分盤')}</p></div>`;
            } else {
                topSides.forEach(rec => {
                    const cardHtml = `
                        <div class="top-rec-list-item" style="--hover-glow-color: var(--accent-blue); --hover-shadow-color: rgba(0, 176, 255, 0.15);" onclick="scrollToMatch('match-card-${rec.matchup_id}')">
                            <div class="top-rec-item-left">
                                <div class="top-rec-logo-container">
                                    <img src="${rec.logo}" class="top-rec-logo" onerror="this.style.display='none'" />
                                </div>
                                <div class="top-rec-item-info">
                                    <span class="top-rec-item-match">
                                        ${translateText(rec.matchup_name)} • ${rec.market_type}
                                        ${rec.is_day_game ? `<span style="color: #fbbf24; font-weight: 700; margin-left: 6px;">⚠️ 下午場</span>` : ''}
                                    </span>
                                    <span class="top-rec-item-bet">${translateText(rec.recommendation)} ${recentFormPill(rec)}</span>
                                </div>
                            </div>
                            <div class="top-rec-item-right">
                                <span class="top-rec-item-roi" style="${scoreBadgeStyle(rec.score)}">保守 ${rec.score}%</span>
                            </div>
                        </div>
                    `;
                    sidesContainer.insertAdjacentHTML('beforeend', cardHtml);
                });
            }

            const totalsContainer = document.getElementById('top-totals-container');
            totalsContainer.innerHTML = '';
            
            if (topTotals.length === 0) {
                totalsContainer.innerHTML = `<div class="no-data-card" style="padding: 24px;"><p style="font-size: 13px; color: var(--text-muted); font-style: italic;">${emptyStateText('大小分總分')}</p></div>`;
            } else {
                topTotals.forEach(rec => {
                    const cardHtml = `
                        <div class="top-rec-list-item" style="--hover-glow-color: var(--accent-green); --hover-shadow-color: rgba(0, 230, 118, 0.15);" onclick="scrollToMatch('match-card-${rec.matchup_id}')">
                            <div class="top-rec-item-left">
                                <div class="top-rec-logo-container">
                                    <img src="${rec.logo_a}" class="top-rec-logo" onerror="this.style.display='none'" />
                                    <img src="${rec.logo_b}" class="top-rec-logo" onerror="this.style.display='none'" />
                                </div>
                                <div class="top-rec-item-info">
                                    <span class="top-rec-item-match">
                                        ${translateText(rec.matchup_name)} • ${rec.market_type}
                                        ${rec.is_day_game ? `<span style="color: #fbbf24; font-weight: 700; margin-left: 6px;">⚠️ 下午場</span>` : ''}
                                    </span>
                                    <span class="top-rec-item-bet">${translateText(rec.recommendation)} ${recentFormPill(rec)}</span>
                                </div>
                            </div>
                            <div class="top-rec-item-right">
                                <span class="top-rec-item-roi" style="${scoreBadgeStyle(rec.score)}">保守 ${rec.score}%</span>
                            </div>
                        </div>
                    `;
                    totalsContainer.insertAdjacentHTML('beforeend', cardHtml);
                });
            }
        }

        function scrollToMatch(id) {
            const el = document.getElementById(id);
            if (el) {
                if (!el.classList.contains('expanded')) {
                    el.classList.add('expanded');
                    syncAccordionHeight(el);
                }
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.classList.add('highlight-flash');
                setTimeout(() => {
                    el.classList.remove('highlight-flash');
                }, 2000);
            }
        }

        // 切換頁籤
        function switchTab(tab, element) {
            currentTab = tab;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            element.classList.add('active');
            renderMatchups();
        }

        // 搜尋隊伍名稱與清除按鈕處理
        function handleSearch() {
            const box = document.getElementById('search-box');
            const clearBtn = document.getElementById('search-clear');
            searchQuery = box.value.trim().toLowerCase();
            if (clearBtn) {
                clearBtn.style.display = searchQuery ? 'flex' : 'none';
            }
            renderMatchups();
        }

        // 清除搜尋框內容
        function clearSearch() {
            const box = document.getElementById('search-box');
            const clearBtn = document.getElementById('search-clear');
            if (box) {
                box.value = '';
            }
            if (clearBtn) {
                clearBtn.style.display = 'none';
            }
            searchQuery = '';
            renderMatchups();
        }

        // 監聽滾動以顯示/隱藏「回到頂部」按鈕
        window.addEventListener('scroll', () => {
            const btn = document.getElementById('back-to-top-btn');
            if (btn) {
                if (window.scrollY > 400) {
                    btn.classList.add('show');
                } else {
                    btn.classList.remove('show');
                }
            }
        });

        // 平滑滾動到頂部
        function scrollToTop() {
            window.scrollTo({
                top: 0,
                behavior: 'smooth'
            });
        }

        // 依實際內容高度設定摺疊區的 max-height（收合時清成空字串讓 CSS 的 0 生效）。
        // 不能用固定值：窄螢幕是單欄排版，趨勢兩欄加上近期走勢會超過任何寫死的數字，
        // 超出的部分會被 overflow: hidden 吃掉，看起來像「只剩前一兩條」。
        function syncTrackHeight() {
            const section = document.getElementById('track-record');
            const body = document.getElementById('tr-body');
            if (section && body && section.classList.contains('expanded')) {
                body.style.maxHeight = body.scrollHeight + 'px';
            }
        }

        function syncAccordionHeight(cardElement) {
            const content = cardElement.querySelector('.accordion-content');
            if (!content) return;
            if (cardElement.classList.contains('expanded')) {
                content.style.maxHeight = content.scrollHeight + 'px';
            } else {
                content.style.maxHeight = '';
            }
        }

        // 展開/收合卡片摺疊區
        function toggleExpand(cardElement) {
            cardElement.classList.toggle('expanded');
            syncAccordionHeight(cardElement);
        }

        // 轉向橫式或改變視窗寬度時欄數會變，已展開的卡片要重新量一次
        window.addEventListener('resize', () => {
            document.querySelectorAll('.match-card.expanded').forEach(syncAccordionHeight);
            syncTrackHeight();
        });

        // 渲染賽事清單
        function renderMatchups() {
            const container = document.getElementById('matchups-container');
            container.innerHTML = '';
            
            const filtered = allMatchups.filter(m => {
                if (currentTab === 'double' && m.double_positive.length === 0) return false;
                if (currentTab === 'opposing' && m.opposing_trends.length === 0) return false;
                
                if (searchQuery) {
                    const titleEn = (m.team_a + " vs " + m.team_b).toLowerCase();
                    const titleZh = (translateText(m.team_a) + " vs " + translateText(m.team_b)).toLowerCase();
                    if (!titleEn.includes(searchQuery) && !titleZh.includes(searchQuery)) return false;
                }
                
                return true;
            });

            if (filtered.length === 0) {
                container.innerHTML = `
                    <div class="no-data-card">
                        <div class="no-data-icon">🛸</div>
                        <h2>無符合條件的對戰組合</h2>
                        <p>請嘗試清除搜尋詞或切換其他分類頁籤。</p>
                    </div>
                `;
                return;
            }

            filtered.forEach(m => {
                const doubleTags = m.double_positive.length > 0 ? `<span class="match-tag double-tag">🔥 大小分總分 (${m.double_positive.length})</span>` : '';
                const opposingTags = m.opposing_trends.length > 0 ? `<span class="match-tag opposing-tag">🎯 勝負/讓分盤 (${m.opposing_trends.length})</span>` : '';
                const dayGameTag = m.is_day_game 
                    ? `<span class="match-tag day-game-tag">\u26a0\ufe0f \u4e0b\u5348\u5834</span>` 
                    : '';
                
                let dayGameBanner = '';
                if (m.is_day_game) {
                    const bannerText = '<strong>此賽事為下午場 (Day Game)</strong>：主場當地時間 17:00 前開打（多數落在 13:00~14:00）。下午場由於球員生理時鐘、陣容輪替(主力休息、備用捕手先發)與牛棚調度等變數極多，盤口<strong>極易開出反邊</strong>，建議<strong>避開</strong>或考慮<strong>反下</strong>。';
                    dayGameBanner = `
                        <div class="day-game-banner">
                            <span class="day-game-icon">⚠️</span>
                            <div class="day-game-text">
                                ${bannerText}
                            </div>
                        </div>
                    `;
                }
                
                let recsHtml = '';
                
                if (m.double_positive.length > 0 && (currentTab === 'all' || currentTab === 'double')) {
                    recsHtml += `
                        <div class="section-title">
                            <span>🔥 大小分總分黃金推薦</span>
                        </div>
                        <div class="rec-container">
                    `;
                    m.double_positive.forEach(rec => {
                        recsHtml += `
                            <div class="rec-box double-box">
                                <div class="rec-title-row">
                                    <span class="rec-type-badge">大小分總分 • ${rec.market_type}</span>
                                    <span class="roi-badge" style="${scoreBadgeStyle(rec.score)}">過盤: ${rec.hit_detail} | 保守 ${rec.score}%</span>
                                </div>
                                <div class="rec-headline">${translateText(rec.recommendation)}</div>
                                <div class="rec-desc">${translateText(rec.confidence)}</div>
                                ${recentFormFlag(m, rec.direction)}
                            </div>
                        `;
                    });
                    recsHtml += `</div>`;
                }
                
                if (m.opposing_trends.length > 0 && (currentTab === 'all' || currentTab === 'opposing')) {
                    recsHtml += `
                        <div class="section-title">
                            <span>🎯 勝負/讓分盤黃金推薦</span>
                        </div>
                        <div class="rec-container">
                    `;
                    m.opposing_trends.forEach(rec => {
                        recsHtml += `
                            <div class="rec-box opposing-box">
                                <div class="rec-title-row">
                                    <span class="rec-type-badge">勝負/讓分盤 • ${rec.market_zh}</span>
                                    <span class="roi-badge" style="${scoreBadgeStyle(rec.score)}">過盤: ${rec.hit_detail} | 保守 ${rec.score}%</span>
                                </div>
                                <div class="rec-headline">${translateText(rec.recommendation)}</div>
                                <div class="rec-desc">${translateText(rec.confidence)}</div>
                                ${recentFormSideFlag(m, rec)}
                            </div>
                        `;
                    });
                    recsHtml += `</div>`;
                }

                if (m.double_positive.length === 0 && m.opposing_trends.length === 0) {
                    const cardEmptyText = (m.processed_trends || []).length === 0
                        ? '⏳ covers.com 尚未發佈此賽事的趨勢，稍後自動更新會補上。'
                        : '此賽事今日無符合篩選標準的黃金推薦投注組合。';
                    recsHtml += `
                        <div style="padding: 10px 0; color: var(--text-muted); font-size: 13px; font-style: italic;">
                            ${cardEmptyText}
                        </div>
                    `;
                }

                let teamATrendsHtml = '';
                let teamBTrendsHtml = '';
                
                const highTrendsA = m.processed_trends.filter(t => t.team === m.team_a && t.class === 'High');
                const lowTrendsA = m.processed_trends.filter(t => t.team === m.team_a && t.class === 'Low');
                const highTrendsB = m.processed_trends.filter(t => t.team === m.team_b && t.class === 'High');
                const lowTrendsB = m.processed_trends.filter(t => t.team === m.team_b && t.class === 'Low');

                const trendRow = t => {
                    const klassName = t.class === 'High' ? 'trend-high' : 'trend-low';
                    return `
                        <li class="trend-item ${klassName}">
                            <span class="trend-class-dot"></span>
                            <div>${renderTrendText(t)}</div>
                        </li>
                    `;
                };

                [...highTrendsA, ...lowTrendsA].forEach(t => { teamATrendsHtml += trendRow(t); });
                [...highTrendsB, ...lowTrendsB].forEach(t => { teamBTrendsHtml += trendRow(t); });

                if (!teamATrendsHtml) teamATrendsHtml = '<li class="trend-item" style="color: var(--text-muted);">無趨勢數據</li>';
                if (!teamBTrendsHtml) teamBTrendsHtml = '<li class="trend-item" style="color: var(--text-muted);">無趨勢數據</li>';

                // Recent Form 參考區：樣本 4~9 場且幾乎全勝，是 covers 篩選過的結果，只顯示不計分
                let recentFormHtml = '';
                const rf = m.recent_form || [];
                if (rf.length > 0) {
                    const lean = m.rf_lean || {};
                    let leanTag = '';
                    if (lean.lean) {
                        const leanZh = lean.lean === 'Over' ? '大分' : '小分';
                        leanTag = `<span class="rf-lean-tag">近期偏 ${leanZh}（大 ${lean.over} / 小 ${lean.under}）</span>`;
                    } else {
                        leanTag = `<span class="rf-lean-tag rf-lean-mixed">方向分歧（大 ${lean.over || 0} / 小 ${lean.under || 0}）</span>`;
                    }
                    const rows = rf.map(t => {
                        let dirClass = 'rf-neutral';
                        if (t.side === 'Over') dirClass = 'rf-over';
                        else if (t.side === 'Under') dirClass = 'rf-under';
                        return `<li class="rf-item ${dirClass}"><span class="rf-dot"></span><div>${renderRecentFormText(t.zh)}</div></li>`;
                    }).join('');
                    recentFormHtml = `
                        <div class="rf-block">
                            <div class="rf-head">
                                <span class="rf-title">📈 近期走勢（僅供參考）</span>
                                ${leanTag}
                            </div>
                            <p class="rf-caveat">
                                covers 從大量條件切法中挑出的連勝紀錄，樣本僅 4~9 場、幾乎都是全勝，
                                <strong>是被挑過的結果而非隨機樣本</strong>，因此不列入保守命中率評分，只當旁證看。
                                此區只有<strong>大小分</strong>與<strong>直接勝負</strong>兩種市場，covers 未提供讓分走勢——
                                讓分盤的標記是借用直接勝負推得：<strong>受讓 1.5</strong> 直接獲勝即必定過盤（有效），
                                <strong>讓分 1.5</strong> 還需贏 2 分以上（僅標為較弱）。
                            </p>
                            <ul class="rf-list">${rows}</ul>
                        </div>
                    `;
                }

                const cardHtml = `
                    <div class="match-card" id="match-card-${m.path.split('/').pop()}">
                        <div class="match-header">
                            <div class="match-meta-left">
                                <div class="teams-versus">
                                <span class="team-name-badge">
                                    <img src="${m.team_a_logo}" class="team-logo" onerror="this.style.display='none'" />
                                    ${translateText(m.team_a)}
                                </span>
                                <span class="vs-text">vs</span>
                                <span class="team-name-badge">
                                    <img src="${m.team_b_logo}" class="team-logo" onerror="this.style.display='none'" />
                                    ${translateText(m.team_b)}
                                </span>
                            </div>
                                <div class="match-time-sub">\U0001f552 ${formatGameTime(m)}</div>
                            </div>
                            <div class="match-tags">
                                ${dayGameTag}
                                ${doubleTags}
                                ${opposingTags}
                            </div>
                        </div>
                        
                        <div class="match-body">
                            ${dayGameBanner}
                            ${recsHtml}
                            
                            <button class="details-trigger" onclick="toggleExpand(this.closest('.match-card'))">
                                <span>顯示該賽事完整詳細趨勢數據 (High / Low Trends)</span>
                                <svg width="12" height="12" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            
                            <div class="accordion-content">
                                <div class="accordion-inner">
                                    <div class="team-trends-col">
                                        <h4>
                                            <img src="${m.team_a_logo}" class="team-logo" style="width: 24px; height: 24px; border-radius: 6px; padding: 2px;" onerror="this.style.display='none'" />
                                            ${translateText(m.team_a)} 趨勢數據
                                        </h4>
                                        <ul class="trend-list">
                                            ${teamATrendsHtml}
                                        </ul>
                                    </div>
                                    <div class="team-trends-col">
                                        <h4>
                                            <img src="${m.team_b_logo}" class="team-logo" style="width: 24px; height: 24px; border-radius: 6px; padding: 2px;" onerror="this.style.display='none'" />
                                            ${translateText(m.team_b)} 趨勢數據
                                        </h4>
                                        <ul class="trend-list">
                                            ${teamBTrendsHtml}
                                        </ul>
                                    </div>
                                </div>
                                ${recentFormHtml}
                            </div>
                        </div>
                    </div>
                `;
                container.insertAdjacentHTML('beforeend', cardHtml);
            });
        }"""


def render_track_record(track):
    """
    頁面底部的「過往命中率」區。沒有資料時回空字串——整區不輸出，不佔版面。

    刻意的幾件事：
    - 分母和百分比一樣大。17 筆的 41% 和 170 筆的 41% 是兩回事，只寫百分比會把雜訊當訊號。
    - 一併顯示保守命中率（Wilson 下界），與推薦卡片同一套算法與用語。
    - 預設收合，放在最下面。網站在台灣 22:00 的任務是「今天買什麼」，戰績是事後參考。
    """
    if not track:
        return ''

    def cell(stat):
        if not stat:
            return '<td class="tr-empty">—</td>'
        return (f'<td><span class="tr-rate">{stat["rate"]}%</span>'
                f'<span class="tr-n">{stat["hit"]}/{stat["total"]}</span></td>')

    rows = []
    overall_recent = track['recent'].get('總計')
    overall_all = track['all'].get('總計')
    rows.append('<tr class="tr-total"><th>AI Top 5 合計</th>'
                + cell(overall_recent) + cell(overall_all) + '</tr>')
    for key in track['markets']:
        recent = track['recent'].get(key)
        whole = track['all'].get(key)
        if not recent and not whole:
            continue
        label = track['market_zh'].get(key, key)
        rows.append(f'<tr><th>{label}</th>' + cell(recent) + cell(whole) + '</tr>')

    def split_rows(names):
        out = []
        for name in names:
            stat = track['splits'].get(name)
            if not stat:
                continue
            out.append(
                f'<tr><th>{name}</th>'
                f'<td><span class="tr-rate">{stat["rate"]}%</span>'
                f'<span class="tr-n">{stat["hit"]}/{stat["total"]}</span></td></tr>')
        return out

    splits_block = ''
    day_rows = split_rows(['非下午場', '下午場'])
    rf_rows = split_rows(['走勢同向', '走勢反向', '走勢無方向'])
    parts = []
    if day_rows:
        parts.append(
            '<div class="tr-split"><h4>依時段（全期間）</h4>'
            '<table class="tr-table"><tbody>' + ''.join(day_rows) + '</tbody></table></div>')
    if rf_rows:
        since = track.get('rf_from') or ''
        note = f'（{since} 起才有近期走勢資料）' if since else ''
        parts.append(
            f'<div class="tr-split"><h4>依近期走勢{note}</h4>'
            '<table class="tr-table"><tbody>' + ''.join(rf_rows) + '</tbody></table></div>')
    if parts:
        splits_block = '<div class="tr-splits">' + ''.join(parts) + '</div>'

    yesterday_block = ''
    y = track.get('yesterday')
    if y:
        marks = ''.join('<span class="tr-mark hit">\u2705</span>' if ok
                        else '<span class="tr-mark miss">\u274c</span>' for ok in y['marks'])
        hit = sum(1 for ok in y['marks'] if ok)
        month_day = '/'.join(str(int(x)) for x in y['date'].split('-')[1:])
        yesterday_block = (
            '<div class="tr-yesterday">'
            f'<span class="tr-yesterday-label">最近一次結算（{month_day}）</span>'
            f'<span class="tr-marks">{marks}</span>'
            f'<span class="tr-yesterday-score">{hit}/{len(y["marks"])}</span>'
            '</div>')

    return f"""
        <section class="track-record" id="track-record">
            <button class="tr-toggle" onclick="toggleTrackRecord()" aria-expanded="false">
                <span class="tr-title">\U0001f4ca 過往命中率</span>
                <span class="tr-sub">{track['days']} 天 / {overall_all['total']} 筆推薦</span>
                <span class="tr-caret" id="tr-caret">\u25be</span>
            </button>
            <div class="tr-body" id="tr-body">
                {yesterday_block}
                <div class="tr-table-wrap">
                    <table class="tr-table">
                        <thead><tr><th></th><th>近 {track['recent_days']} 天</th><th>全期間</th></tr></thead>
                        <tbody>{''.join(rows)}</tbody>
                    </table>
                </div>
                {splits_block}
                <p class="tr-note">
                    這是「有沒有過盤」的紀錄，<strong>不含賠率</strong>——命中率高不等於賺錢。
                    樣本數少的欄位波動很大，<strong>請一併看分母</strong>——
                    8 筆的 50% 和 70 筆的 50% 完全是兩回事。
                    此區純粹顯示，<strong>不影響任何推薦與排序</strong>。
                </p>
            </div>
        </section>
"""


def generate_html_dashboard(matchups_data, top_5_sides, top_5_totals, top_5_ai, date_str,
                            failed_count=0, expected_count=None, track_record=None):
    """
    將爬取與運算後的結果導出為一個極具質感的本機互動式繁體中文 HTML 網頁。

    failed_count / expected_count 用來在頁面上明示「有幾場沒抓到」——抓取失敗原本是
    完全靜默的（那場直接從資料裡消失），使用者只會看到今天少了一場，無從分辨是
    covers 沒排還是我們漏抓。
    """
    track_section = render_track_record(track_record)
    total_matches = len(matchups_data)
    total_double_pos = sum(len(m['double_positive']) for m in matchups_data)
    total_opposing = sum(len(m['opposing_trends']) for m in matchups_data)

    # 抓取失敗警告列（沒失敗時完全不輸出，不佔版面）
    fetch_warning = ''
    if failed_count:
        expected = expected_count if expected_count else total_matches + failed_count
        fetch_warning = (
            '<div class="fetch-warning">'
            f'⚠️ <strong>今日有 {failed_count} 場沒有抓取成功</strong>'
            f'（賽事列表共 {expected} 場，實際分析 {total_matches} 場）。'
            '缺少的場次不會出現在下方清單與推薦中，並非當天沒有該場比賽——'
            '通常是 covers.com 暫時無回應，下一輪自動更新會重試。'
            '</div>'
        )

    display_date = date_str if date_str else datetime.now().strftime("%Y-%m-%d")
    tw_dates = taiwan_play_dates(display_date, matchups_data)
    # 只標美東日期會讓台灣使用者以為賽事已結束（他看的當地日期通常已是隔天）
    tw_hint = f'<span class="date-badge-tw">台灣 {tw_dates} 開打</span>' if tw_dates else ''
    
    # JS 唯一需要 Python 值的地方：門檻常數（見 TOP_PICK_MIN_SCORE）
    dashboard_js = DASHBOARD_JS.replace("__TOP_PICK_MIN_SCORE__", str(TOP_PICK_MIN_SCORE))

    matchups_json = json.dumps(matchups_data, ensure_ascii=False)
    top_sides_json = json.dumps(top_5_sides, ensure_ascii=False)
    top_totals_json = json.dumps(top_5_totals, ensure_ascii=False)
    top_ai_json = json.dumps(top_5_ai, ensure_ascii=False)
    
    html_template = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MLB 每日賽事黃金趨勢篩選系統</title>
    <!-- Google Fonts Inter 字型與思源黑體。
         以 media="print" + onload 切換載入，**不阻塞首次繪製**：原本是一般
         stylesheet，瀏覽器要等 Google 回應才畫第一個像素，而 Noto Sans TC 是
         中文字型、檔案不小——使用者在台灣用手機看，這是最可能拖慢首屏的一項。
         字型還沒到之前先用系統字型（font-family 尾端有 sans-serif 墊底），
         到了再無縫換上。noscript 分支保留給關掉 JS 的情況。 -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" media="print" onload="this.media='all'; this.onload=null;"
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Noto+Sans+TC:wght@300;400;500;700;900&display=swap">
    <noscript>
        <link rel="stylesheet"
              href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Noto+Sans+TC:wght@300;400;500;700;900&display=swap">
    </noscript>

    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <div class="container">
        <!-- 頂部導航與標題 -->
        <header>
            <div class="header-top">
                <h1>MLB 每日賽事黃金趨勢篩選 <span>PRO v{APP_VERSION}</span></h1>
                <div class="header-actions">
                    <div class="date-badge">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
                        賽事日期：<strong>{display_date}</strong><span class="date-badge-et">美東</span>
                        {tw_hint}
                    </div>
                </div>
            </div>
            
            <!-- 統計數據面板 -->
            <div class="stats-grid">
                <div class="stats-card stats-total">
                    <div class="stats-info">
                        <h3>當日對戰組合數</h3>
                        <p id="stat-total-matches">{total_matches}</p>
                    </div>
                    <div class="stats-icon">🏟️</div>
                </div>
                <div class="stats-card stats-double">
                    <div class="stats-info">
                        <h3>🔥 大小分總分推薦數</h3>
                        <p id="stat-double-recs">{total_double_pos}</p>
                    </div>
                    <div class="stats-icon">🔥</div>
                </div>
                <div class="stats-card stats-opposing">
                    <div class="stats-info">
                        <h3>🎯 勝負/讓分盤推薦數</h3>
                        <p id="stat-opposing-recs">{total_opposing}</p>
                    </div>
                    <div class="stats-icon">🎯</div>
                </div>
            </div>
        </header>

        {fetch_warning}

        <!-- 今日 AI 精選 Top 5 推薦專區 -->
        <section class="ai-section" id="ai-top5-section">
            <!-- 由 JS 動態渲染 -->
        </section>

        <!-- 今日雙欄 Top 5 黃金投注推薦專區 -->
        <section class="top-section">
            <div class="top-columns-grid">
                <!-- 勝負/讓分盤專區 -->
                <div class="top-col">
                    <h2 class="section-main-title">
                        <span class="pulse-glow">⚡</span> 今日「勝負/讓分盤」Top 5 黃金推薦
                    </h2>
                    <div class="top-rec-vertical-list" id="top-sides-container">
                        <!-- 由 JS 動態渲染 -->
                    </div>
                </div>
                
                <!-- 大小分總分專區 -->
                <div class="top-col">
                    <h2 class="section-main-title">
                        <span class="pulse-glow-green" style="color: var(--accent-green);">🔥</span> 今日「大小分總分」Top 5 黃金推薦
                    </h2>
                    <div class="top-rec-vertical-list" id="top-totals-container">
                        <!-- 由 JS 動態渲染 -->
                    </div>
                </div>
            </div>
        </section>

        <!-- 差一點進 Top 5、但近期走勢同向的向隅推薦（無符合者由 JS 隱藏） -->
        <section class="near-miss-section" id="near-miss-section" style="display: none;"></section>

        <!-- 篩選與控制列 -->
        <div class="controls-bar">
            <div class="tabs">
                <button class="tab-btn active" onclick="switchTab('all', this)">
                    全部對戰 <span class="tab-count" id="count-all">{total_matches}</span>
                </button>
                <button class="tab-btn" onclick="switchTab('double', this)">
                    🔥 大小分總分 <span class="tab-count" id="count-double">{total_double_pos}</span>
                </button>
                <button class="tab-btn" onclick="switchTab('opposing', this)">
                    🎯 勝負/讓分盤 <span class="tab-count" id="count-opposing">{total_opposing}</span>
                </button>
            </div>
            
            <div class="search-wrapper">
                <svg class="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                <input type="text" class="search-input" id="search-box" placeholder="搜尋球隊名稱..." oninput="handleSearch()">
                <button class="search-clear-btn" id="search-clear" onclick="clearSearch()">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
            </div>
        </div>

        <!-- 賽事對戰組合清單 -->
        <div class="match-grid" id="matchups-container">
            <!-- 賽事卡片將由此處經由 Javascript 動態渲染 -->
        </div>
    </div>

    <!-- 回到頂部按鈕 -->
    <button class="back-to-top" id="back-to-top-btn" onclick="scrollToTop()">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"></polyline></svg>
    </button>

    {track_section}

    <footer>
        數據抓取自 covers.com • 本地動態儀表板 • 僅供分析參考，請理性投注
    </footer>

    <!-- 嵌入 JSON 賽事數據 -->
    <script id="matchups-data" type="application/json">
        {matchups_json}
    </script>
    <script id="top-sides-data" type="application/json">
        {top_sides_json}
    </script>
    <script id="top-totals-data" type="application/json">
        {top_totals_json}
    </script>
    <script id="top-ai-data" type="application/json">
        {top_ai_json}
    </script>

    <script>
{dashboard_js}

    </script>
</body>
</html>
"""
    # 寫入 HTML 檔案
    output_filenames = ["index.html"]
    for output_filename in output_filenames:
        try:
            with open(output_filename, "w", encoding="utf-8") as f:
                f.write(html_template)
            print(f"[+] 成功生成繁體中文 HTML 互動儀表板：{output_filename}")
        except Exception as e:
            print(f"[錯誤] 無法寫入 HTML 儀表板文件 {output_filename}: {e}")

def load_known_game_times(date_str, path="index.html"):
    """
    從既有 index.html 讀回「上一輪已經抓到的開賽時間」，key 為 matchup id。

    比賽一旦開打，賽事列表與單場頁面**都**拿不到開賽時間（單場頁面的 startDate
    只有賽前才有，進行中會變成 InProgress）。但每天跑六輪，早一輪多半在賽前就抓到了，
    直接沿用即可——開賽時間本來就不太會變。
    只有在既有頁面的賽事日期與本次相同時才沿用，避免把昨天的時間帶到今天。
    """
    try:
        with open(path, encoding="utf-8") as f:
            html_text = f.read()
    except OSError:
        return {}

    stamped = re.search(r'賽事日期：<strong>(\d{4}-\d{2}-\d{2})</strong>', html_text)
    if not stamped or stamped.group(1) != date_str:
        return {}

    block = re.search(r'id="matchups-data"[^>]*>(.*?)</script>', html_text, re.DOTALL)
    if not block:
        return {}
    try:
        previous = json.loads(block.group(1))
    except json.JSONDecodeError:
        return {}

    known = {}
    for item in previous:
        game_time = item.get('game_time')
        path_value = item.get('path') or ''
        if game_time and game_time != 'None' and path_value:
            known[path_value.rsplit('/', 1)[-1]] = game_time
    return known


def replay_from_html(path="index.html"):
    """
    不連網，從既有 index.html 內嵌的 JSON 重新產生 HTML（`python scrape.py --replay`）。

    純粹是為了改 UI：跑一次完整爬蟲要 16 次 HTTP 請求、等一分鐘，還會多打擾 covers 一次；
    只是改一行 CSS 卻要付這個代價，改版時會很痛。頁面本來就內嵌了四份 JSON
    （matchups-data / top-sides-data / top-totals-data / top-ai-data），
    剛好就是 generate_html_dashboard 的四個資料參數，直接讀回來重畫即可。

    因為輸入輸出都是同一份資料，**重跑必須產生位元組完全相同的檔案**——
    這也讓它可以拿來驗證「只改樣板、不改行為」的重構有沒有改壞東西。
    """
    try:
        with open(path, encoding="utf-8") as f:
            html_text = f.read()
    except OSError as e:
        print(f"[錯誤] 無法讀取 {path}：{e}")
        print("      --replay 需要先有一份既有的 index.html（請先正常跑一次）。")
        return

    def grab(elem_id):
        m = re.search(r'id="%s"[^>]*>(.*?)</script>' % elem_id, html_text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as err:
            print(f"[錯誤] {elem_id} 的內嵌 JSON 解析失敗：{err}")
            return None

    matchups_data = grab("matchups-data")
    if matchups_data is None:
        print("[錯誤] 找不到可用的 matchups-data，無法 replay。")
        return
    top_sides = grab("top-sides-data") or []
    top_totals = grab("top-totals-data") or []
    top_ai = grab("top-ai-data") or []

    m_date = re.search(r'賽事日期：<strong>(\d{4}-\d{2}-\d{2})</strong>', html_text)
    date_str = m_date.group(1) if m_date else get_eastern_today()

    # 抓取失敗警告列只有失敗時才會輸出，沒有就代表當時全部抓成功
    failed_count, expected_count = 0, None
    m_warn = re.search(r'今日有 (\d+) 場沒有抓取成功（賽事列表共 (\d+) 場', html_text)
    if m_warn:
        failed_count, expected_count = int(m_warn.group(1)), int(m_warn.group(2))

    print(f"[*] Replay 模式：從 {path} 讀回 {len(matchups_data)} 場賽事資料（{date_str}），不連網。")
    print(f"    Top 5 勝負 {len(top_sides)} 筆 / 大小分 {len(top_totals)} 筆 / AI {len(top_ai)} 筆"
          + (f" / 當時有 {failed_count} 場抓取失敗" if failed_count else ""))
    track_record = compute_track_record(load_history(), date_str)
    generate_html_dashboard(matchups_data, top_sides, top_totals, top_ai, date_str,
                            track_record=track_record,
                            failed_count=failed_count, expected_count=expected_count)


# ==========================================
# 主流程控制
# ==========================================
def main():
    print("====================================================")
    print("      MLB 賽事趨勢爬蟲與智能黃金推薦篩選系統")
    print("====================================================")
    
    # 改 UI 用的離線重畫模式：不連網，直接用既有 index.html 的資料重新產生頁面
    if '--replay' in sys.argv:
        replay_from_html()
        return

    # 檢查是否有指定日期參數
    date_str = None
    if len(sys.argv) > 1:
        for i, arg in enumerate(sys.argv):
            if arg == '--date' and i + 1 < len(sys.argv):
                date_str = sys.argv[i+1]
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                    print("[警告] 指定的日期格式不正確，應為 YYYY-MM-DD。將採用今日賽事。")
                    date_str = None

    # 未指定日期時，明確採用「美東今天」，避免 covers.com 預設頁回傳前一日已完賽賽事
    if not date_str:
        date_str = get_eastern_today()

    # 比賽開打後就抓不到開賽時間了，先把上一輪已知的讀回來備用
    known_game_times = load_known_game_times(date_str)

    # 1. 抓取對戰清單與球隊縮寫
    matchups_list = get_matchups_data(date_str)
    if not matchups_list:
        print("[!] 當日無賽事或無法取得賽事清單，程式結束。")
        return
        
    all_matchups_data = []
    failed_matchups = []   # 抓取/解析失敗的場次，用於在網頁上顯示警告

    # 2. 迴圈抓取每對賽事的 picks 頁面並進行解析
    for i, matchup in enumerate(matchups_list):
        print(f"\n[{i+1}/{len(matchups_list)}] 正在處理對戰組合...")
        
        matchup_data = parse_matchup_details(matchup)
        if not matchup_data:
            # 靜默失敗是這個專案最危險的失敗模式：該場會整場從當日資料消失，
            # 網站上不會有任何跡象。記下來，最後交給前端顯示警告。
            print(f"  [跳過] 無法抓取或解析該場對戰: {matchup['path']}")
            failed_matchups.append(matchup['path'].split('/')[-1])
            continue
            
        # 賽事列表與單場頁面都沒有開賽時間時（比賽已開打），沿用上一輪抓到的
        if matchup_data.get('game_time') in (None, '', 'None'):
            recalled = known_game_times.get(matchup['path'].rsplit('/', 1)[-1])
            if recalled:
                matchup_data['game_time'] = recalled
                matchup_data['is_day_game'] = is_day_game(recalled, matchup.get('home_short', ''))

        print(f"  對戰雙方: {matchup_data['team_a']} vs {matchup_data['team_b']}")
        print(f"  原始趨勢數: {len(matchup_data['trends'])} 條")
        
        # 3. 標準化分類趨勢
        processed_trends = classify_and_process_trends(matchup_data)
        
        # 4. 智能篩選媒合 (大小分與獨贏/讓分)
        double_pos, opposing = analyze_betting_recommendations(matchup_data, processed_trends)
        
        print(f"  -> 篩選出 [大小分總分]: {len(double_pos)} 項 | [勝負/讓分盤]: {len(opposing)} 盤口")
        
        # 4.5 Recent Form：僅供參考顯示與方向佐證，不參與評分排序
        recent_form = parse_recent_form(
            matchup_data.get('recent_form', []),
            matchup_data['team_a'], matchup_data['team_b'])
        rf_lean = recent_form_lean(recent_form)

        local_time, taiwan_time = game_time_variants(
            matchup_data['game_time'], matchup.get('home_short', ''), date_str)

        all_matchups_data.append({
            'path': matchup_data['path'],
            'team_a': matchup_data['team_a'],
            'team_b': matchup_data['team_b'],
            'team_a_logo': matchup_data['team_a_logo'],
            'team_b_logo': matchup_data['team_b_logo'],
            'processed_trends': processed_trends,
            'double_positive': double_pos,
            'opposing_trends': opposing,
            'recent_form': recent_form,
            'rf_lean': rf_lean,
            'game_time': matchup_data['game_time'],
            'local_time': local_time,
            'taiwan_time': taiwan_time,
            'is_day_game': matchup_data['is_day_game']
        })
        
        time.sleep(1.0)
        
    # 5. 彙整並分類所有的推薦組合
    sides_recs = []
    totals_recs = []
    
    for matchup in all_matchups_data:
        m_id = matchup['path'].split('/')[-1]
        m_name = f"{matchup['team_a']} vs {matchup['team_b']}"
        
        # 收集大小分總分推薦 (原雙向正面)
        for rec in matchup['double_positive']:
            totals_recs.append({
                'matchup_id': m_id,
                'matchup_name': m_name,
                'type': 'double',
                'type_zh': '大小分總分',
                'market_type': rec['market_type'],
                'direction': rec.get('direction'),  # 供前端比對近期走勢方向
                'rf_agree': recent_form_agreement(matchup, rec),  # 僅供同分決勝
                'recommendation': rec['recommendation'],
                'confidence': rec['confidence'],
                'score': rec['score'],
                'hit_detail': rec['hit_detail'],
                'logo_a': matchup['team_a_logo'],
                'logo_b': matchup['team_b_logo'],
                'details': f"過盤紀錄: {rec['hit_detail']}",
                'is_day_game': matchup['is_day_game'],
                'game_time': matchup['game_time']
            })
            
        # 收集勝負/讓分盤推薦 (原一正一反)
        for rec in matchup['opposing_trends']:
            logo_url = matchup['team_a_logo'] if rec['bet_on'] == matchup['team_a'] else matchup['team_b_logo']
            sides_recs.append({
                'matchup_id': m_id,
                'matchup_name': m_name,
                'type': 'opposing',
                'type_zh': '勝負/讓分盤',
                'market_type': rec['market_zh'],
                'recommendation': rec['recommendation'],
                'confidence': rec['confidence'],
                'score': rec['score'],
                'hit_detail': rec['hit_detail'],
                'bet_on': rec['bet_on'],
                'bet_against': rec['bet_against'],  # 供前端比對近期走勢
                'market': rec['market'],
                'spread_side': rec.get('spread_side'),
                'rf_agree': recent_form_agreement(matchup, rec),  # 僅供同分決勝
                'logo': logo_url,
                'details': f"過盤紀錄: {rec['hit_detail']}",
                'is_day_game': matchup['is_day_game'],
                'game_time': matchup['game_time']
            })
            
    # 精選清單先去除「同場同隊」的重複推薦（獨贏與讓分常成對出現，是同一個看法）
    sides_for_top = dedupe_same_team_picks(sides_recs)
    # 大小分不需要對應處理：單場最多 1 筆，源頭的「全場大小分避碰」已經決定方向
    totals_for_top = totals_recs

    # 低於 TOP_PICK_MIN_SCORE 的推薦不進任何精選清單：網站自己標示 <50% 為可忽略，
    # 讓它上榜等於自打嘴巴。寧可當日列不滿 5 筆（前端有對應空狀態文案）。
    def qualified(recs):
        return [r for r in recs if (r.get('score') or 0) >= TOP_PICK_MIN_SCORE]

    # 分別對兩組推薦以「命中率 Wilson 下界」由大到小排序，取各自的前 5 名 (Top 5)
    # 同分時才用近期走勢決勝（見 recent_form_agreement 說明），否則純看保守命中率
    # rf_agree 是三態：True 同向 / None 無明顯方向 / False 反向。
    # 舊寫法 `0 if x.get('rf_agree') else 1` 會把 False 和 None 併成同一組，
    # 等於「走勢明確唱反調」和「走勢沒意見」同等對待。這個決勝點常被用到——
    # 實測第 5 與第 6 名有 14% 的日子完全同分、64% 相差不到 1 分（中位數 0.5 分）。
    rank_key = lambda x: (-x['score'], _RF_RANK.get(x.get('rf_agree'), 1))
    top_5_sides = sorted(qualified(sides_for_top), key=rank_key)[:5]
    top_5_totals = sorted(qualified(totals_for_top), key=rank_key)[:5]

    # 5.5 計算「今日 AI 推薦 Top 5」 (排除真正互斥的同場推薦，依命中率下界排序)
    by_matchup = {}
    for r in sides_for_top:
        by_matchup.setdefault(r['matchup_id'], []).append(r)

    conflicting_matchups = {
        m_id for m_id, recs in by_matchup.items() if picks_are_contradictory(recs)
    }

    ai_candidates = []
    for r in sides_for_top:
        if r['matchup_id'] in conflicting_matchups:
            continue
        ai_candidates.append({
            'matchup_id': r['matchup_id'],
            'matchup_name': r['matchup_name'],
            'type': 'opposing',
            'type_zh': '勝負/讓分盤',
            'market_type': r['market_type'],
            'recommendation': r['recommendation'],
            'confidence': r['confidence'],
            'score': r['score'],
            'hit_detail': r['hit_detail'],
            'bet_on': r['bet_on'],
            'bet_against': r.get('bet_against'),
            'market': r.get('market'),
            'spread_side': r.get('spread_side'),
            'rf_agree': r.get('rf_agree'),
            'logo_a': r['logo'],
            'logo_b': None,
            'rationale': f"黃金對立組合！優勢隊 {r['bet_on']} 近期過盤 {r['hit_detail']}，保守命中率 {r['score']}%，強弱差距顯著。",
            'is_day_game': r['is_day_game'],
            'game_time': r['game_time']
        })
        
    for r in totals_for_top:
        ai_candidates.append({
            'matchup_id': r['matchup_id'],
            'matchup_name': r['matchup_name'],
            'type': 'double',
            'type_zh': '大小分總分',
            'market_type': r['market_type'],
            'direction': r.get('direction'),
            'rf_agree': r.get('rf_agree'),
            'recommendation': r['recommendation'],
            'confidence': r['confidence'],
            'score': r['score'],
            'hit_detail': r['hit_detail'],
            'logo_a': r['logo_a'],
            'logo_b': r['logo_b'],
            'rationale': f"雙向強勢指標！兩隊近期在 {r['market_type']} 盤口高度吻合，過盤紀錄 {r['hit_detail']}，保守命中率 {r['score']}%。",
            'is_day_game': r['is_day_game'],
            'game_time': r['game_time']
        })
        
    top_5_ai = sorted(qualified(ai_candidates), key=rank_key)[:5]
    
    print(f"\n[+] 成功計算出今日「勝負/讓分盤」與「大小分總分」雙欄 Top 5 黃金投注推薦。")
    print(f"[+] 成功計算出今日「AI 推薦 Top 5」精選：{len(top_5_ai)} 項組合。")
    if failed_matchups:
        print(f"[!] 有 {len(failed_matchups)} 場抓取失敗，網頁上會顯示警告：{failed_matchups}")

    # 6. 累積戰績：記下今天的推薦，並補抓前幾天的比賽結果
    history = load_history()
    record_daily_picks(history, date_str, all_matchups_data, top_5_ai)
    backfill_results(history, date_str)
    save_history(history)
    track_record = compute_track_record(history, date_str)
    if track_record:
        overall = track_record['all'].get('總計')
        if overall:
            print(f"[+] 戰績累積：{track_record['days']} 天，Top 5 過盤 "
                  f"{overall['hit']}/{overall['total']}（{overall['rate']}%）")

    # 7. 生成動態互動式 HTML 數據儀表板
    generate_html_dashboard(all_matchups_data, top_5_sides, top_5_totals, top_5_ai, date_str,
                            failed_count=len(failed_matchups),
                            expected_count=len(matchups_list),
                            track_record=track_record)
    
    print("====================================================")
    print("                  抓取與分析完成！")
    print("====================================================")

if __name__ == "__main__":
    main()
