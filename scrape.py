import urllib.request
import re
import html
import json
import time
import os
import sys
from datetime import datetime

# ==========================================
# 網路請求模組與防擋策略
# ==========================================
def fetch_url(url):
    """
    使用自訂瀏覽器標頭發送 HTTP 請求，防止被 covers.com 封鎖。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  [錯誤] 無法抓取網頁 {url}: {e}")
        return None

def parse_run_lines(html_str):
    """
    從 covers.com matchup picks 頁面 HTML 中解析兩隊的全場讓分 (Run Line) 與隊伍簡寫，
    並判斷誰是讓分方 (-) 與受讓方 (+)。同時解析全場大小分總分盤口。
    """
    try:
        rl_indices = [m.start() for m in re.finditer(r'Game Line - Run Line - FT', html_str)]
        if not rl_indices:
            return None
        
        idx = rl_indices[0]
        block = html_str[max(0, idx-1000):min(len(html_str), idx+2000)]
        
        team_a_match = re.search(r'class="other-over-odds th-label"[^>]*>\s*([A-Za-z0-9]+)\s*</', block, re.IGNORECASE)
        team_b_match = re.search(r'class="other-under-odds th-label"[^>]*>\s*([A-Za-z0-9]+)\s*</', block, re.IGNORECASE)
        
        if not team_a_match or not team_b_match:
            return None
            
        team_a_abbr = team_a_match.group(1).upper()
        team_b_abbr = team_b_match.group(1).upper()
        
        post_rl = block[block.find("Game Line - Run Line - FT"):]
        col_a_match = re.search(r'class="other-over-odds"[^>]*>.*?<div class="odds upper-block">.*?<span>\s*(.*?)\s*</span>', post_rl, re.DOTALL | re.IGNORECASE)
        col_b_match = re.search(r'class="other-under-odds"[^>]*>.*?<div class="odds upper-block">.*?<span>\s*(.*?)\s*</span>', post_rl, re.DOTALL | re.IGNORECASE)
        
        if not col_a_match or not col_b_match:
            return None
            
        spread_a = col_a_match.group(1).strip()
        spread_b = col_b_match.group(1).strip()
        
        spread_a = html.unescape(spread_a).replace('&#x2B;', '+')
        spread_b = html.unescape(spread_b).replace('&#x2B;', '+')
        
        # 解析全場大小總分盤口值
        total_line = None
        tot_indices = [m.start() for m in re.finditer(r'Game Line - Total - FT', html_str)]
        if tot_indices:
            idx_tot = tot_indices[0]
            block_tot = html_str[idx_tot:idx_tot+1000]
            tot_match = re.search(r'<span>\s*[ou](\d+(?:\.\d+)?)\s*</span>', block_tot, re.IGNORECASE)
            if tot_match:
                total_line = tot_match.group(1).strip()
        
        return {
            'team_a': team_a_abbr,
            'team_b': team_b_abbr,
            'spread_a': spread_a,
            'spread_b': spread_b,
            'total_line': total_line
        }
    except Exception as e:
        print(f"  [警告] 提取盤口讓分值與大小值時發生錯誤: {e}")
        return None

# ==========================================
# 賽事抓取與路徑提取
# ==========================================
def get_matchups_data(date_str=None):
    """
    抓取 MLB 每日賽事列表，並提取當日所有獨特的對戰頁面 ID、路徑與球隊 Logo 縮寫。
    """
    base_url = 'https://www.covers.com/sports/mlb/matchups'
    if date_str:
        target_url = f"{base_url}?date={date_str}"
        print(f"[*] 正在抓取指定日期 {date_str} 的賽事列表...")
    else:
        target_url = base_url
        print("[*] 正在抓取今日的 MLB 賽事列表...")
        
    html_content = fetch_url(target_url)
    if not html_content:
        print("[錯誤] 無法獲取賽事列表，請檢查網路連線。")
        return []
        
    # 尋找所有包含 gamebox class 的 article 標籤
    article_pattern = re.compile(r'<article\s+([^>]*class="[^"]*gamebox[^"]*"[^>]*)>', re.IGNORECASE | re.DOTALL)
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
        
        matchups.append({
            'id': game_id,
            'path': f"/sport/baseball/mlb/matchup/{game_id}",
            'away_short': away_short,
            'home_short': home_short
        })
        
    print(f"[+] 成功從賽事列表解析到 {len(matchups)} 場對戰與球隊 Logo 縮寫。")
    return matchups

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
            vs_match = re.search(r'(.*?)\s+vs\s+([^\s]+)', title_text)
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
        'trends': raw_trends
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
    roi_pattern = re.compile(r'([+-]?\d+)%\s+ROI', re.IGNORECASE)
    
    for trend in matchup['trends']:
        text = trend['text']
        text_lower = text.lower()
        
        # 排除所有首五局 (F5) 的趨勢與球隊大小分 (Team Total) 趨勢，只保留全場的
        if "1st five innings" in text_lower or " f5 " in text_lower or "first five" in text_lower or "f5" in text_lower:
            continue
        if "team total" in text_lower:
            continue
            
        klass = trend['class']
        
        # 1. 判定該趨勢屬於哪支球隊
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

        # 2. 提取 Units 與 ROI
        units_match = units_pattern.search(text)
        roi_match = roi_pattern.search(text)
        
        units = float(units_match.group(1)) if units_match else 0.0
        roi = int(roi_match.group(1)) if roi_match else 0
        
        # 3. 判定盤口市場 (Market)
        market = "Other"
        if "1st five innings (f5)" in text_lower or " f5 " in text_lower:
            if "team total" in text_lower:
                market = "F5 Team Total"
            elif "game total" in text_lower:
                market = "F5 Game Total"
            elif "moneyline" in text_lower:
                market = "F5 Moneyline"
            elif "run line" in text_lower or "runline" in text_lower:
                market = "F5 Run Line"
        else:
            if "team total" in text_lower:
                market = "Team Total"
            elif "game total" in text_lower:
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
            'class': klass,
            'text': text,
            'market': market,
            'direction': direction,
            'units': units,
            'roi': roi
        })
        
    return processed_trends

# ==========================================
# 智能趨勢篩選演算法 (核心投注推薦邏輯)
# ==========================================
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
    
    # --- 1. 大小分總分趨勢媒合 (全場大小分) ---
    total_line = matchup.get('total_line')
    
    # A. 全場大小分 (Full Game Totals)
    high_under_full = [t for t in processed_trends if t['class'] == 'High' and t['direction'] == 'Under' and t['market'] == 'Game Total']
    high_over_full = [t for t in processed_trends if t['class'] == 'High' and t['direction'] == 'Over' and t['market'] == 'Game Total']
    
    a_under_full = [t for t in high_under_full if t['team'] == team_a]
    b_under_full = [t for t in high_under_full if t['team'] == team_b]
    
    under_full_rec = None
    if a_under_full and b_under_full:
        under_full_rec = {
            'market_type': f"Under (小 {total_line})" if total_line else 'Under (全場小分)',
            'recommendation': f"買 全場小分 (小 {total_line})" if total_line else '買 全場小分 (Game Under)',
            'confidence': f"雙正面強勢指標：{team_a} 擁有 {len(a_under_full)} 項全場 Under 趨勢，{team_b} 擁有 {len(b_under_full)} 項全場 Under 趨勢。",
            'team_a_trends': [t['text'] for t in a_under_full],
            'team_b_trends': [t['text'] for t in b_under_full],
            'avg_roi': round((sum(t['roi'] for t in a_under_full + b_under_full) / len(a_under_full + b_under_full)), 1)
        }
        
    a_over_full = [t for t in high_over_full if t['team'] == team_a]
    b_over_full = [t for t in high_over_full if t['team'] == team_b]
    
    over_full_rec = None
    if a_over_full and b_over_full:
        over_full_rec = {
            'market_type': f"Over (大 {total_line})" if total_line else 'Over (全場大分)',
            'recommendation': f"買 全場大分 (大 {total_line})" if total_line else '買 全場大分 (Game Over)',
            'confidence': f"雙正面強勢指標：{team_a} 擁有 {len(a_over_full)} 項全場 Over 趨勢，{team_b} 擁有 {len(b_over_full)} 項全場 Over 趨勢。",
            'team_a_trends': [t['text'] for t in a_over_full],
            'team_b_trends': [t['text'] for t in b_over_full],
            'avg_roi': round((sum(t['roi'] for t in a_over_full + b_over_full) / len(a_over_full + b_over_full)), 1)
        }
        
    # 全場大小分避碰 (若同場全場同時推薦大分與小分，只保留高 ROI 者)
    if under_full_rec and over_full_rec:
        if under_full_rec['avg_roi'] >= over_full_rec['avg_roi']:
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
        a_trends = [t for t in processed_trends if t['team'] == team_a and t['market'] == m]
        b_trends = [t for t in processed_trends if t['team'] == team_b and t['market'] == m]
        
        # 情況 A: A 隊極強 (High), B 隊極弱 (Low)
        a_strong = [t for t in a_trends if t['class'] == 'High' and (t['direction'] in ['Win', 'Cover'])]
        b_weak = [t for t in b_trends if t['class'] == 'Low' and (t['direction'] in ['Lose', 'Fail to Cover'])]
        
        if a_strong and b_weak:
            # 動態解析該隊是讓分還是受讓，並附上具體讓分值
            if m == 'Run Line':
                m_zh = get_spread_detail(team_a_spread, team_a_side)
            else:
                m_zh = market_zh_map.get(m, m)
                
            opposing_trends.append({
                'market': m,
                'market_zh': m_zh,
                'bet_on': team_a,
                'bet_against': team_b,
                'recommendation': f"買 {team_a} {m_zh}",
                'confidence': f"黃金一正一反組合：{team_a} 在 {m_zh} 表現極強（+{a_strong[0]['units']} Units），而對手 {team_b} 表現極差（{b_weak[0]['units']} Units）。",
                'strong_trend': a_strong[0]['text'],
                'weak_trend': b_weak[0]['text'],
                'roi_diff': a_strong[0]['roi'] - b_weak[0]['roi'],
                'strong_roi': a_strong[0]['roi']
            })
            
        # 情況 B: B 隊極強 (High), A 隊極弱 (Low)
        b_strong = [t for t in b_trends if t['class'] == 'High' and (t['direction'] in ['Win', 'Cover'])]
        a_weak = [t for t in a_trends if t['class'] == 'Low' and (t['direction'] in ['Lose', 'Fail to Cover'])]
        
        if b_strong and a_weak:
            # 動態解析該隊是讓分還是受讓，並附上具體讓分值
            if m == 'Run Line':
                m_zh = get_spread_detail(team_b_spread, team_b_side)
            else:
                m_zh = market_zh_map.get(m, m)
                
            opposing_trends.append({
                'market': m,
                'market_zh': m_zh,
                'bet_on': team_b,
                'bet_against': team_a,
                'recommendation': f"買 {team_b} {m_zh}",
                'confidence': f"黃金一正一反組合：{team_b} 在 {m_zh} 表現極強（+{b_strong[0]['units']} Units），而對手 {team_a} 表現極差（{a_weak[0]['units']} Units）。",
                'strong_trend': b_strong[0]['text'],
                'weak_trend': a_weak[0]['text'],
                'roi_diff': b_strong[0]['roi'] - a_weak[0]['roi'],
                'strong_roi': b_strong[0]['roi']
            })
            
    return double_positive, opposing_trends

# ==========================================
# 質感中文化 HTML 儀表板文件生成器
# ==========================================
def generate_html_dashboard(matchups_data, top_5_sides, top_5_totals, top_3_ai, date_str):
    """
    將爬取與運算後的結果導出為一個極具質感的本機互動式繁體中文 HTML 網頁。
    """
    total_matches = len(matchups_data)
    total_double_pos = sum(len(m['double_positive']) for m in matchups_data)
    total_opposing = sum(len(m['opposing_trends']) for m in matchups_data)
    
    display_date = date_str if date_str else datetime.now().strftime("%Y-%m-%d")
    
    matchups_json = json.dumps(matchups_data, ensure_ascii=False)
    top_sides_json = json.dumps(top_5_sides, ensure_ascii=False)
    top_totals_json = json.dumps(top_5_totals, ensure_ascii=False)
    top_ai_json = json.dumps(top_3_ai, ensure_ascii=False)
    
    html_template = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MLB 每日賽事黃金趨勢篩選系統</title>
    <!-- Google Fonts Inter 字型與思源黑體 -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Noto+Sans+TC:wght@300;400;500;700;900&display=swap" rel="stylesheet">
    
    <style>
        /* ==========================================
           1. 變數與基礎樣式 (Core Palette & Reset)
           ========================================== */
        :root {{
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
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
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
        }}

        .container {{
            max-width: 1280px;
            margin: 0 auto;
            padding: 0 24px;
        }}

        /* ==========================================
           2. 頂部導航欄與統計區 (Header & Statistics)
           ========================================== */
        header {{
            padding: 40px 0 20px 0;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 30px;
        }}

        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }}

        h1 {{
            font-size: 28px;
            font-weight: 800;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #ffffff 30%, #a5b4fc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        h1 span {{
            font-size: 14px;
            font-weight: 600;
            color: var(--accent-orange);
            background: rgba(253, 80, 0, 0.1);
            border: 1px solid rgba(253, 80, 0, 0.3);
            padding: 4px 10px;
            border-radius: 99px;
            letter-spacing: 0;
            -webkit-text-fill-color: var(--accent-orange);
        }}

        .date-badge {{
            font-size: 14px;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 8px;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .date-badge strong {{
            color: var(--text-primary);
        }}

        /* 統計面板網格 */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }}

        .stats-card {{
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
        }}

        .stats-card:hover {{
            transform: translateY(-2px);
            border-color: rgba(255, 255, 255, 0.15);
        }}

        .stats-info h3 {{
            font-size: 14px;
            color: var(--text-secondary);
            font-weight: 500;
            margin-bottom: 8px;
        }}

        .stats-info p {{
            font-size: 32px;
            font-weight: 800;
        }}

        .stats-icon {{
            width: 48px;
            height: 48px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
        }}

        .stats-total .stats-icon {{
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
        }}

        .stats-double .stats-icon {{
            background: var(--accent-green-glow);
            color: var(--accent-green);
            border: 1px solid rgba(0, 230, 118, 0.2);
        }}

        .stats-opposing .stats-icon {{
            background: rgba(0, 176, 255, 0.1);
            color: var(--accent-blue);
            border: 1px solid rgba(0, 176, 255, 0.2);
        }}

        /* ==========================================
           3. 今日雙欄 Top 5 黃金投注推薦專區 (Top 5 Columns)
           ========================================== */
        .top-section {{
            margin-bottom: 45px;
        }}

        .top-columns-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 28px;
        }}

        @media (max-width: 992px) {{
            .top-columns-grid {{
                grid-template-columns: 1fr;
                gap: 30px;
            }}
        }}

        .top-col {{
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .section-main-title {{
            font-size: 18px;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--text-primary);
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 12px;
        }}

        .pulse-glow {{
            animation: pulse 1.5s infinite alternate;
            font-size: 20px;
        }}

        .pulse-glow-green {{
            animation: pulse-green 1.5s infinite alternate;
            font-size: 20px;
        }}

        @keyframes pulse {{
            0% {{ transform: scale(1); filter: drop-shadow(0 0 2px rgba(0,176,255,0.5)); }}
            100% {{ transform: scale(1.15); filter: drop-shadow(0 0 8px rgba(0,176,255,0.9)); }}
        }}

        @keyframes pulse-green {{
            0% {{ transform: scale(1); filter: drop-shadow(0 0 2px rgba(0,230,118,0.5)); }}
            100% {{ transform: scale(1.15); filter: drop-shadow(0 0 8px rgba(0,230,118,0.9)); }}
        }}

        .top-rec-vertical-list {{
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        /* 橫向列表項目 (Sleek List Item Card) */
        .top-rec-list-item {{
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
        }}

        .top-rec-list-item:hover {{
            transform: translateX(6px);
            border-color: var(--hover-glow-color, var(--accent-blue));
            box-shadow: 0 4px 15px var(--hover-shadow-color, rgba(0, 176, 255, 0.15));
            background: rgba(255, 255, 255, 0.02);
        }}

        .top-rec-item-left {{
            display: flex;
            align-items: center;
            gap: 12px;
            overflow: hidden;
            flex-grow: 1;
        }}

        .top-rec-logo-container {{
            display: flex;
            align-items: center;
            position: relative;
            height: 38px;
            flex-shrink: 0;
        }}

        .top-rec-logo {{
            width: 34px;
            height: 34px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 4px;
        }}

        .top-rec-logo-container .top-rec-logo:nth-child(2) {{
            margin-left: -12px;
            position: relative;
            z-index: 2;
            background: rgba(18, 25, 38, 0.95);
        }}

        .top-rec-item-info {{
            display: flex;
            flex-direction: column;
            gap: 2px;
            overflow: hidden;
        }}

        .top-rec-item-match {{
            font-size: 10px;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
        }}

        .top-rec-item-bet {{
            font-size: 14px;
            font-weight: 800;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .top-rec-item-right {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }}

        .top-rec-item-roi {{
            font-size: 12px;
            font-weight: 800;
            padding: 4px 8px;
            border-radius: 6px;
        }}

        /* ==========================================
           4. 篩選與控制欄 (Filters & Controls)
           ========================================== */
        .controls-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
            margin-bottom: 30px;
            border-top: 1px solid var(--border-color);
            padding-top: 30px;
        }}

        /* 分類頁籤 */
        .tabs {{
            display: flex;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 4px;
            border-radius: 12px;
            gap: 4px;
        }}

        .tab-btn {{
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
        }}

        .tab-btn:hover {{
            color: var(--text-primary);
        }}

        .tab-btn.active {{
            background: var(--accent-orange);
            color: #ffffff;
            box-shadow: 0 4px 15px var(--accent-orange-glow);
        }}

        .tab-count {{
            font-size: 11px;
            background: rgba(255, 255, 255, 0.15);
            padding: 2px 6px;
            border-radius: 99px;
            color: #ffffff;
        }}

        .tab-btn.active .tab-count {{
            background: rgba(0, 0, 0, 0.2);
        }}

        /* 搜尋輸入框 */
        .search-wrapper {{
            position: relative;
            max-width: 320px;
            width: 100%;
        }}

        .search-input {{
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
        }}

        .search-input:focus {{
            border-color: var(--accent-orange);
            background: rgba(255, 255, 255, 0.05);
            box-shadow: 0 0 15px rgba(253, 80, 0, 0.15);
        }}

        .search-icon {{
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            font-size: 16px;
            pointer-events: none;
        }}

        /* ==========================================
           5. 對戰卡片網格與詳情 (Match Grid & Accordion)
           ========================================== */
        .match-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 24px;
        }}

        .match-card {{
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: var(--shadow-premium);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}

        .match-card:hover {{
            border-color: rgba(255, 255, 255, 0.12);
            box-shadow: 0 15px 35px -5px rgba(0, 0, 0, 0.6);
        }}

        /* 高亮閃爍動畫 (滾動錨點交互) */
        @keyframes borderFlash {{
            0%, 100% {{ border-color: var(--border-color); box-shadow: var(--shadow-premium); }}
            50% {{ border-color: var(--accent-orange); box-shadow: 0 0 35px rgba(253, 80, 0, 0.5); }}
        }}

        .highlight-flash {{
            animation: borderFlash 2s ease;
        }}

        /* 卡片頭部：隊伍與對戰名稱 */
        .match-header {{
            padding: 24px 28px;
            border-bottom: 1px solid var(--border-color);
            background: rgba(255, 255, 255, 0.01);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }}

        .teams-versus {{
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }}

        .team-logo {{
            width: 38px;
            height: 38px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 4px;
            flex-shrink: 0;
        }}

        .team-name-badge {{
            font-size: 20px;
            font-weight: 800;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .vs-text {{
            font-size: 13px;
            font-weight: 700;
            color: var(--text-muted);
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 4px 8px;
            border-radius: 6px;
            text-transform: uppercase;
        }}

        .match-tags {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .match-tag {{
            font-size: 12px;
            font-weight: 700;
            padding: 6px 12px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .match-tag.double-tag {{
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid rgba(0, 230, 118, 0.3);
            color: var(--accent-green);
        }}

        .match-tag.opposing-tag {{
            background: rgba(0, 176, 255, 0.1);
            border: 1px solid rgba(0, 176, 255, 0.3);
            color: var(--accent-blue);
        }}

        /* 核心推薦區域 */
        .match-body {{
            padding: 28px;
        }}

        .section-title {{
            font-size: 14px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .rec-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }}

        @media (max-width: 768px) {{
            .rec-container {{
                grid-template-columns: 1fr;
            }}
        }}

        /* 推薦卡片樣式 */
        .rec-box {{
            border-radius: 14px;
            padding: 20px;
            border: 1px dashed transparent;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.15);
        }}

        /* 大小分總分推薦箱 */
        .rec-box.double-box {{
            background: radial-gradient(circle at top right, rgba(0, 230, 118, 0.05), transparent 60%), rgba(255, 255, 255, 0.02);
            border-color: rgba(0, 230, 118, 0.25);
        }}

        .rec-box.double-box::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--accent-green);
        }}

        /* 勝負/讓分盤推薦箱 */
        .rec-box.opposing-box {{
            background: radial-gradient(circle at top right, rgba(0, 176, 255, 0.05), transparent 60%), rgba(255, 255, 255, 0.02);
            border-color: rgba(0, 176, 255, 0.25);
        }}

        .rec-box.opposing-box::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--accent-blue);
        }}

        .rec-title-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .rec-type-badge {{
            font-size: 12px;
            font-weight: 800;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
        }}

        .double-box .rec-type-badge {{
            background: var(--accent-green);
            color: #000000;
        }}

        .opposing-box .rec-type-badge {{
            background: var(--accent-blue);
            color: #000000;
        }}

        .roi-badge {{
            font-size: 13px;
            font-weight: 700;
            color: var(--accent-green);
            background: rgba(0, 230, 118, 0.1);
            padding: 4px 8px;
            border-radius: 6px;
        }}

        .rec-headline {{
            font-size: 16px;
            font-weight: 800;
            color: var(--text-primary);
        }}

        .rec-desc {{
            font-size: 13px;
            color: var(--text-secondary);
            line-height: 1.6;
        }}

        /* 動態收合摺疊區 (Accordion Details) */
        .details-trigger {{
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
        }}

        .details-trigger:hover {{
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
            border-color: rgba(255, 255, 255, 0.15);
        }}

        .details-trigger svg {{
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            fill: currentColor;
        }}

        .match-card.expanded .details-trigger {{
            background: rgba(255, 255, 255, 0.04);
            border-bottom-left-radius: 0;
            border-bottom-right-radius: 0;
            color: var(--text-primary);
        }}

        .match-card.expanded .details-trigger svg {{
            transform: rotate(180deg);
            color: var(--accent-orange);
        }}

        .accordion-content {{
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            background: rgba(255, 255, 255, 0.015);
            border-left: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
            border-bottom: 1px solid var(--border-color);
            border-bottom-left-radius: 12px;
            border-bottom-right-radius: 12px;
        }}

        .match-card.expanded .accordion-content {{
            max-height: 1500px;
        }}

        .accordion-inner {{
            padding: 24px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
        }}

        @media (max-width: 768px) {{
            .accordion-inner {{
                grid-template-columns: 1fr;
            }}
        }}

        /* 隊伍趨勢清單 */
        .team-trends-col h4 {{
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 16px;
            border-left: 3px solid var(--accent-orange);
            padding-left: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .trend-list {{
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .trend-item {{
            background: rgba(255, 255, 255, 0.01);
            border: 1px solid rgba(255, 255, 255, 0.04);
            border-radius: 10px;
            padding: 14px 16px;
            font-size: 13px;
            line-height: 1.5;
            display: flex;
            align-items: flex-start;
            gap: 12px;
        }}

        .trend-item.trend-high {{
            border-left: 3px solid var(--accent-green);
        }}

        .trend-item.trend-low {{
            border-left: 3px solid var(--accent-red);
            color: var(--text-secondary);
        }}

        .trend-class-dot {{
            width: 8px;
            height: 8px;
            border-radius: 99px;
            margin-top: 5px;
            flex-shrink: 0;
        }}

        .trend-high .trend-class-dot {{
            background: var(--accent-green);
            box-shadow: 0 0 8px var(--accent-green-glow);
        }}

        .trend-low .trend-class-dot {{
            background: var(--accent-red);
            box-shadow: 0 0 8px var(--accent-red-glow);
        }}

        /* ==========================================
           6. 空狀態與無數據提示
           ========================================== */
        .no-data-card {{
            background: var(--surface-color);
            backdrop-filter: var(--glass-blur);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 60px 40px;
            text-align: center;
            box-shadow: var(--shadow-premium);
        }}

        .no-data-icon {{
            font-size: 48px;
            margin-bottom: 20px;
        }}

        .no-data-card h2 {{
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text-primary);
        }}

        .no-data-card p {{
            font-size: 14px;
            color: var(--text-secondary);
        }}

        /* ==========================================
           AI 精選推薦專區 (AI Top 3 Section)
           ========================================== */
        .ai-section {{
            margin-bottom: 45px;
            background: linear-gradient(135deg, rgba(168, 85, 247, 0.08) 0%, rgba(253, 80, 0, 0.04) 100%);
            border: 1px solid rgba(168, 85, 247, 0.2);
            border-radius: 24px;
            padding: 28px;
            box-shadow: 0 15px 35px -10px rgba(168, 85, 247, 0.15);
            backdrop-filter: var(--glass-blur);
        }}

        .ai-title-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}

        .ai-main-title {{
            font-size: 20px;
            font-weight: 800;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 10px;
            text-shadow: 0 0 10px rgba(168, 85, 247, 0.4);
        }}

        .ai-badge-glow {{
            background: linear-gradient(135deg, #a855f7 0%, #fd5000 100%);
            color: #ffffff;
            font-size: 11px;
            font-weight: 700;
            padding: 4px 12px;
            border-radius: 99px;
            box-shadow: 0 0 12px rgba(168, 85, 247, 0.5);
            letter-spacing: 0.5px;
        }}

        .ai-cards-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
        }}

        @media (max-width: 992px) {{
            .ai-cards-grid {{
                grid-template-columns: 1fr;
                gap: 20px;
            }}
        }}

        .ai-card {{
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
        }}

        .ai-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 3px;
            background: linear-gradient(90deg, #a855f7, #fd5000);
            opacity: 0.8;
        }}

        .ai-card:hover {{
            transform: translateY(-5px);
            border-color: rgba(168, 85, 247, 0.4);
            box-shadow: 0 10px 25px -5px rgba(168, 85, 247, 0.15);
            background: rgba(15, 23, 42, 0.7);
        }}

        .ai-card-rank {{
            position: absolute;
            right: 16px;
            top: 16px;
            font-size: 28px;
            font-weight: 900;
            color: rgba(255, 255, 255, 0.03);
            font-style: italic;
            line-height: 1;
        }}

        .ai-card-header {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .ai-card-tag {{
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
        }}

        .ai-card-tag.side-tag {{
            background: rgba(0, 176, 255, 0.15);
            color: var(--accent-blue);
            border: 1px solid rgba(0, 176, 255, 0.25);
        }}

        .ai-card-tag.total-tag {{
            background: rgba(0, 230, 118, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(0, 230, 118, 0.25);
        }}

        .ai-card-match {{
            font-size: 12px;
            color: var(--text-muted);
            font-weight: 600;
        }}

        .ai-card-bet {{
            font-size: 16px;
            font-weight: 800;
            color: var(--text-primary);
            margin-top: 4px;
        }}

        .ai-card-logos {{
            display: flex;
            align-items: center;
            height: 30px;
        }}

        .ai-card-logo {{
            width: 28px;
            height: 28px;
            object-fit: contain;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            padding: 3px;
        }}

        .ai-card-logos .ai-card-logo:nth-child(2) {{
            margin-left: -10px;
            background: rgba(8, 12, 20, 0.95);
        }}

        .ai-card-rationale {{
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.5;
            background: rgba(255, 255, 255, 0.02);
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid rgba(255, 255, 255, 0.03);
            flex-grow: 1;
        }}

        .ai-card-footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            padding-top: 10px;
            margin-top: 4px;
        }}

        .ai-card-roi-label {{
            font-size: 11px;
            color: var(--text-muted);
            font-weight: 600;
        }}

        .ai-card-roi-val {{
            font-size: 16px;
            font-weight: 800;
            color: var(--accent-gold);
            text-shadow: 0 0 8px rgba(255, 210, 0, 0.3);
        }}

        /* ==========================================
           7. 頁尾
           ========================================== */
        footer {{
            margin-top: 60px;
            text-align: center;
            color: var(--text-muted);
            font-size: 12px;
            letter-spacing: 0.5px;
        }}

        /* 語言切換按鈕與排版 */
        .header-actions {{
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .lang-toggle-btn {{
            font-size: 14px;
            font-weight: 600;
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.15) 0%, rgba(165, 180, 252, 0.05) 100%);
            border: 1px solid rgba(165, 180, 252, 0.3);
            padding: 8px 16px;
            border-radius: 8px;
            color: #a5b4fc;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            user-select: none;
        }}

        .lang-toggle-btn:hover {{
            border-color: #a5b4fc;
            color: #ffffff;
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.3) 0%, rgba(165, 180, 252, 0.15) 100%);
            box-shadow: 0 0 15px rgba(165, 180, 252, 0.2);
            transform: translateY(-1px);
        }}

        .lang-toggle-btn:active {{
            transform: translateY(1px);
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- 頂部導航與標題 -->
        <header>
            <div class="header-top">
                <h1>MLB 每日賽事黃金趨勢篩選 <span>PRO v1.3</span></h1>
                <div class="header-actions">
                    <div class="date-badge">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
                        賽事日期：<strong>{display_date}</strong>
                    </div>
                    <button class="lang-toggle-btn" id="lang-toggle" onclick="toggleLanguage()">
                        🌐 隊伍名稱：中文
                    </button>
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

        <!-- 今日 AI 精選 Top 3 推薦專區 -->
        <section class="ai-section" id="ai-top3-section">
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
            </div>
        </div>

        <!-- 賽事對戰組合清單 -->
        <div class="match-grid" id="matchups-container">
            <!-- 賽事卡片將由此處經由 Javascript 動態渲染 -->
        </div>
    </div>

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
        // ==========================================
        // MLB 隊伍名稱中英文對照字典
        // ==========================================
        const teamTranslations = {{
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
            "Washington Nationals": "華盛頓國民"
        }};

        let currentLanguage = 'zh'; // 預設使用中文

        // 中英文切換輔助函式
        function translateText(text) {{
            if (!text) return text;
            if (currentLanguage === 'en') {{
                // 英文模式下，將重複的 Athletics Athletics 整理為 Athletics
                return text.replace(/Athletics Athletics/g, "Athletics");
            }}
            let translated = text;
            const sortedKeys = Object.keys(teamTranslations).sort((a, b) => b.length - a.length);
            for (const key of sortedKeys) {{
                const regex = new RegExp(key, 'g');
                translated = translated.replace(regex, teamTranslations[key]);
            }}
            return translated;
        }}

        // 切換語言按鈕點擊事件
        function toggleLanguage() {{
            currentLanguage = currentLanguage === 'zh' ? 'en' : 'zh';
            const btn = document.getElementById('lang-toggle');
            if (btn) {{
                btn.innerHTML = `🌐 隊伍名稱：${{currentLanguage === 'zh' ? '中文' : 'English'}}`;
            }}
            
            // 重新渲染所有內容
            renderAiTop3();
            renderTopLists();
            renderMatchups();
        }}

        // ==========================================
        // 核心前端邏輯 (Core Frontend Script)
        // ==========================================
        let allMatchups = [];
        let topSides = [];
        let topTotals = [];
        let topAi = [];
        let currentTab = 'all';
        let searchQuery = '';

        // 初始化加載數據
        window.addEventListener('DOMContentLoaded', () => {{
            const rawData = document.getElementById('matchups-data').textContent;
            const rawSides = document.getElementById('top-sides-data').textContent;
            const rawTotals = document.getElementById('top-totals-data').textContent;
            const rawAi = document.getElementById('top-ai-data').textContent;
            try {{
                allMatchups = JSON.parse(rawData);
                topSides = JSON.parse(rawSides);
                topTotals = JSON.parse(rawTotals);
                topAi = JSON.parse(rawAi);
                
                renderAiTop3();
                renderTopLists();
                renderMatchups();
            }} catch(e) {{
                console.error("解析 JSON 數據出錯:", e);
                document.getElementById('matchups-container').innerHTML = `
                    <div class="no-data-card">
                        <div class="no-data-icon">⚠️</div>
                        <h2>數據加載錯誤</h2>
                        <p>無法讀取嵌入的 JSON 對戰數據。</p>
                    </div>
                `;
            }}
        }});

        // 渲染今日 AI 精選 Top 3 推薦
        function renderAiTop3() {{
            const section = document.getElementById('ai-top3-section');
            if (!section) return;
            
            if (topAi.length === 0) {{
                section.style.display = 'none';
                return;
            }}
            
            let cardsHtml = '';
            topAi.forEach((rec, idx) => {{
                const rankNum = idx + 1;
                const tagClass = rec.type === 'opposing' ? 'side-tag' : 'total-tag';
                const tagText = rec.type === 'opposing' ? '🎯 勝負/讓分' : '🔥 大小總分';
                const roiLabel = rec.type === 'opposing' ? '優勢隊投報率' : '平均投報率';
                
                let logosHtml = '';
                if (rec.logo_b) {{
                    logosHtml = `
                        <img src="${{rec.logo_a}}" class="ai-card-logo" onerror="this.style.display='none'" />
                        <img src="${{rec.logo_b}}" class="ai-card-logo" onerror="this.style.display='none'" />
                    `;
                }} else {{
                    logosHtml = `
                        <img src="${{rec.logo_a}}" class="ai-card-logo" onerror="this.style.display='none'" />
                    `;
                }}
                
                cardsHtml += `
                    <div class="ai-card" onclick="scrollToMatch('match-card-${{rec.matchup_id}}')">
                        <div class="ai-card-rank">#0${{rankNum}}</div>
                        <div class="ai-card-header">
                            <span class="ai-card-tag ${{tagClass}}">${{tagText}}</span>
                            <span class="ai-card-match">${{translateText(rec.matchup_name)}}</span>
                        </div>
                        <div>
                            <div class="ai-card-bet">${{translateText(rec.recommendation)}}</div>
                        </div>
                        <div class="ai-card-logos">
                            ${{logosHtml}}
                        </div>
                        <div class="ai-card-rationale">
                            ${{translateText(rec.rationale)}}
                        </div>
                        <div class="ai-card-footer">
                            <span class="ai-card-roi-label">${{roiLabel}}</span>
                            <span class="ai-card-roi-val">${{rec.roi}}%</span>
                        </div>
                    </div>
                `;
            }});
            
            section.innerHTML = `
                <div class="ai-title-row">
                    <h2 class="ai-main-title">
                        🤖 今日 AI 智慧精選 Top 3 黃金推薦
                    </h2>
                    <span class="ai-badge-glow">AI OPTIMIZED</span>
                </div>
                <div class="ai-cards-grid">
                    ${{cardsHtml}}
                </div>
            `;
        }}

        // 渲染頂部兩欄 Top 5 黃金投注推薦
        function renderTopLists() {{
            // 1. 渲染「勝負/讓分盤」Top 5
            const sidesContainer = document.getElementById('top-sides-container');
            sidesContainer.innerHTML = '';
            
            if (topSides.length === 0) {{
                sidesContainer.innerHTML = `
                    <div class="no-data-card" style="padding: 24px;">
                        <p style="font-size: 13px; color: var(--text-muted); font-style: italic;">
                            今日暫無符合篩選標準的「勝負/讓分盤」推薦組合。
                        </p>
                    </div>
                `;
            }} else {{
                topSides.forEach(rec => {{
                    const cardHtml = `
                        <div class="top-rec-list-item" style="--hover-glow-color: var(--accent-blue); --hover-shadow-color: rgba(0, 176, 255, 0.15);" onclick="scrollToMatch('match-card-${{rec.matchup_id}}')">
                            <div class="top-rec-item-left">
                                <div class="top-rec-logo-container">
                                    <img src="${{rec.logo}}" class="top-rec-logo" onerror="this.style.display='none'" />
                                </div>
                                <div class="top-rec-item-info">
                                    <span class="top-rec-item-match">${{translateText(rec.matchup_name)}} • ${{rec.market_type}}</span>
                                    <span class="top-rec-item-bet">${{translateText(rec.recommendation)}}</span>
                                </div>
                            </div>
                            <div class="top-rec-item-right">
                                <span class="top-rec-item-roi" style="color: var(--accent-blue); background: rgba(0, 176, 255, 0.12); border: 1px solid rgba(0, 176, 255, 0.25);">ROI: ${{rec.roi}}%</span>
                            </div>
                        </div>
                    `;
                    sidesContainer.insertAdjacentHTML('beforeend', cardHtml);
                }});
            }}

            // 2. 渲染「大小分總分」Top 5
            const totalsContainer = document.getElementById('top-totals-container');
            totalsContainer.innerHTML = '';
            
            if (topTotals.length === 0) {{
                totalsContainer.innerHTML = `
                    <div class="no-data-card" style="padding: 24px;">
                        <p style="font-size: 13px; color: var(--text-muted); font-style: italic;">
                            今日暫無符合篩選標準的「大小分總分」推薦組合。
                        </p>
                    </div>
                `;
            }} else {{
                topTotals.forEach(rec => {{
                    const cardHtml = `
                        <div class="top-rec-list-item" style="--hover-glow-color: var(--accent-green); --hover-shadow-color: rgba(0, 230, 118, 0.15);" onclick="scrollToMatch('match-card-${{rec.matchup_id}}')">
                            <div class="top-rec-item-left">
                                <div class="top-rec-logo-container">
                                    <img src="${{rec.logo_a}}" class="top-rec-logo" onerror="this.style.display='none'" />
                                    <img src="${{rec.logo_b}}" class="top-rec-logo" onerror="this.style.display='none'" />
                                </div>
                                <div class="top-rec-item-info">
                                    <span class="top-rec-item-match">${{translateText(rec.matchup_name)}} • ${{rec.market_type}}</span>
                                    <span class="top-rec-item-bet">${{translateText(rec.recommendation)}}</span>
                                </div>
                            </div>
                            <div class="top-rec-item-right">
                                <span class="top-rec-item-roi" style="color: var(--accent-green); background: rgba(0, 230, 118, 0.12); border: 1px solid rgba(0, 230, 118, 0.25);">平均: ${{rec.roi}}%</span>
                            </div>
                        </div>
                    `;
                    totalsContainer.insertAdjacentHTML('beforeend', cardHtml);
                }});
            }}
        }}

        // 滾動並高亮特定卡片
        function scrollToMatch(id) {{
            const el = document.getElementById(id);
            if (el) {{
                if (!el.classList.contains('expanded')) {{
                    el.classList.add('expanded');
                }}
                el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                el.classList.add('highlight-flash');
                setTimeout(() => {{
                    el.classList.remove('highlight-flash');
                }}, 2000);
            }}
        }}

        // 切換頁籤
        function switchTab(tab, element) {{
            currentTab = tab;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            element.classList.add('active');
            renderMatchups();
        }}

        // 搜尋隊伍名稱
        function handleSearch() {{
            searchQuery = document.getElementById('search-box').value.trim().toLowerCase();
            renderMatchups();
        }}

        // 展開/收合卡片摺疊區
        function toggleExpand(cardElement) {{
            cardElement.classList.toggle('expanded');
        }}

        // 渲染賽事清單
        function renderMatchups() {{
            const container = document.getElementById('matchups-container');
            container.innerHTML = '';
            
            // 篩選數據
            const filtered = allMatchups.filter(m => {{
                // 1. 頁籤篩選
                if (currentTab === 'double' && m.double_positive.length === 0) return false;
                if (currentTab === 'opposing' && m.opposing_trends.length === 0) return false;
                
                // 2. 搜尋字詞篩選
                if (searchQuery) {{
                    const titleEn = (m.team_a + " vs " + m.team_b).toLowerCase();
                    const titleZh = (translateText(m.team_a) + " vs " + translateText(m.team_b)).toLowerCase();
                    if (!titleEn.includes(searchQuery) && !titleZh.includes(searchQuery)) return false;
                }}
                
                return true;
            }});

            // 若無對應數據
            if (filtered.length === 0) {{
                container.innerHTML = `
                    <div class="no-data-card">
                        <div class="no-data-icon">🛸</div>
                        <h2>無符合條件的對戰組合</h2>
                        <p>請嘗試清除搜尋詞或切換其他分類頁籤。</p>
                    </div>
                `;
                return;
            }}

            // 生成卡片 HTML
            filtered.forEach(m => {{
                const doubleTags = m.double_positive.length > 0 ? `<span class="match-tag double-tag">🔥 大小分總分 (${{m.double_positive.length}})</span>` : '';
                const opposingTags = m.opposing_trends.length > 0 ? `<span class="match-tag opposing-tag">🎯 勝負/讓分盤 (${{m.opposing_trends.length}})</span>` : '';
                
                let recsHtml = '';
                
                // 如果有大小分總分推薦，且當前頁籤為全部或大小分總分
                if (m.double_positive.length > 0 && (currentTab === 'all' || currentTab === 'double')) {{
                    recsHtml += `
                        <div class="section-title">
                            <span>🔥 大小分總分黃金推薦</span>
                        </div>
                        <div class="rec-container">
                    `;
                    m.double_positive.forEach(rec => {{
                        recsHtml += `
                            <div class="rec-box double-box">
                                <div class="rec-title-row">
                                    <span class="rec-type-badge">大小分總分 • ${{rec.market_type}}</span>
                                    <span class="roi-badge">平均 ROI: ${{rec.avg_roi}}%</span>
                                </div>
                                <div class="rec-headline">${{translateText(rec.recommendation)}}</div>
                                <div class="rec-desc">${{translateText(rec.confidence)}}</div>
                            </div>
                        `;
                    }});
                    recsHtml += `</div>`;
                }}
                
                // 如果有勝負/讓分盤推薦，且當前頁籤為全部或勝負/讓分盤
                if (m.opposing_trends.length > 0 && (currentTab === 'all' || currentTab === 'opposing')) {{
                    recsHtml += `
                        <div class="section-title">
                            <span>🎯 勝負/讓分盤黃金推薦</span>
                        </div>
                        <div class="rec-container">
                    `;
                    m.opposing_trends.forEach(rec => {{
                        recsHtml += `
                            <div class="rec-box opposing-box">
                                <div class="rec-title-row">
                                    <span class="rec-type-badge">勝負/讓分盤 • ${{rec.market_zh}}</span>
                                    <span class="roi-badge" style="color: var(--accent-blue); background: rgba(0, 176, 255, 0.1); border: 1px solid rgba(0, 176, 255, 0.25);">ROI 差值: ${{rec.roi_diff}}%</span>
                                </div>
                                <div class="rec-headline">${{translateText(rec.recommendation)}}</div>
                                <div class="rec-desc">${{translateText(rec.confidence)}}</div>
                            </div>
                        `;
                    }});
                    recsHtml += `</div>`;
                }}

                // 如果兩個都沒有
                if (m.double_positive.length === 0 && m.opposing_trends.length === 0) {{
                    recsHtml += `
                        <div style="padding: 10px 0; color: var(--text-muted); font-size: 13px; font-style: italic;">
                            此賽事今日無符合篩選標準的黃金推薦投注組合。
                        </div>
                    `;
                }}

                // 生成對戰詳細數據清單
                let teamATrendsHtml = '';
                let teamBTrendsHtml = '';
                
                const highTrendsA = m.processed_trends.filter(t => t.team === m.team_a && t.class === 'High');
                const lowTrendsA = m.processed_trends.filter(t => t.team === m.team_a && t.class === 'Low');
                const highTrendsB = m.processed_trends.filter(t => t.team === m.team_b && t.class === 'High');
                const lowTrendsB = m.processed_trends.filter(t => t.team === m.team_b && t.class === 'Low');

                [...highTrendsA, ...lowTrendsA].forEach(t => {{
                    const klassName = t.class === 'High' ? 'trend-high' : 'trend-low';
                    // 總是保持原始英文，只清理重複的 Athletics Athletics 為 Athletics
                    const cleanText = t.text.replace(/Athletics Athletics/g, "Athletics");
                    teamATrendsHtml += `
                        <li class="trend-item ${{klassName}}">
                            <span class="trend-class-dot"></span>
                            <div>${{cleanText}}</div>
                        </li>
                    `;
                }});

                [...highTrendsB, ...lowTrendsB].forEach(t => {{
                    const klassName = t.class === 'High' ? 'trend-high' : 'trend-low';
                    // 總是保持原始英文，只清理重複的 Athletics Athletics 為 Athletics
                    const cleanText = t.text.replace(/Athletics Athletics/g, "Athletics");
                    teamBTrendsHtml += `
                        <li class="trend-item ${{klassName}}">
                            <span class="trend-class-dot"></span>
                            <div>${{cleanText}}</div>
                        </li>
                    `;
                }});

                if (!teamATrendsHtml) teamATrendsHtml = '<li class="trend-item" style="color: var(--text-muted);">無趨勢數據</li>';
                if (!teamBTrendsHtml) teamBTrendsHtml = '<li class="trend-item" style="color: var(--text-muted);">無趨勢數據</li>';

                const cardHtml = `
                    <div class="match-card" id="match-card-${{m.path.split('/').pop()}}">
                        <!-- 卡片頂部對戰 -->
                        <div class="match-header">
                            <div class="teams-versus">
                                <span class="team-name-badge">
                                    <img src="${{m.team_a_logo}}" class="team-logo" onerror="this.style.display='none'" />
                                    ${{translateText(m.team_a)}}
                                </span>
                                <span class="vs-text">vs</span>
                                <span class="team-name-badge">
                                    <img src="${{m.team_b_logo}}" class="team-logo" onerror="this.style.display='none'" />
                                    ${{translateText(m.team_b)}}
                                </span>
                            </div>
                            <div class="match-tags">
                                ${{doubleTags}}
                                ${{opposingTags}}
                            </div>
                        </div>
                        
                        <!-- 核心推薦區域 -->
                        <div class="match-body">
                            ${{recsHtml}}
                            
                            <!-- 摺疊按鈕與收合詳情 -->
                            <button class="details-trigger" onclick="toggleExpand(this.closest('.match-card'))">
                                <span>顯示該賽事完整詳細趨勢數據 (High / Low Trends)</span>
                                <svg width="12" height="12" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            
                            <div class="accordion-content">
                                <div class="accordion-inner">
                                    <div class="team-trends-col">
                                        <h4>
                                            <img src="${{m.team_a_logo}}" class="team-logo" style="width: 24px; height: 24px; border-radius: 6px; padding: 2px;" onerror="this.style.display='none'" />
                                            ${{translateText(m.team_a)}} 趨勢數據
                                        </h4>
                                        <ul class="trend-list">
                                            ${{teamATrendsHtml}}
                                        </ul>
                                    </div>
                                    <div class="team-trends-col">
                                        <h4>
                                            <img src="${{m.team_b_logo}}" class="team-logo" style="width: 24px; height: 24px; border-radius: 6px; padding: 2px;" onerror="this.style.display='none'" />
                                            ${{translateText(m.team_b)}} 趨勢數據
                                        </h4>
                                        <ul class="trend-list">
                                            ${{teamBTrendsHtml}}
                                        </ul>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                container.insertAdjacentHTML('beforeend', cardHtml);
            }});
        }}
    </script>
</body>
</html>
"""
    # 寫入 HTML 檔案
    output_filename = "mlb_trends.html"
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html_template)
        print(f"\n[+] 成功生成繁體中文 HTML 互動儀表板：{output_filename}")
        print(f"[*] 您可以按兩下打開 `{output_filename}` 在瀏覽器中檢視今日的黃金推薦賽事！\n")
    except Exception as e:
        print(f"[錯誤] 無法寫入 HTML 儀表板文件: {e}")

# ==========================================
# 主流程控制
# ==========================================
def main():
    print("====================================================")
    print("      MLB 賽事趨勢爬蟲與智能黃金推薦篩選系統")
    print("====================================================")
    
    # 檢查是否有指定日期參數
    date_str = None
    if len(sys.argv) > 1:
        for i, arg in enumerate(sys.argv):
            if arg == '--date' and i + 1 < len(sys.argv):
                date_str = sys.argv[i+1]
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                    print("[警告] 指定的日期格式不正確，應為 YYYY-MM-DD。將採用今日賽事。")
                    date_str = None
                    
    # 1. 抓取對戰清單與球隊縮寫
    matchups_list = get_matchups_data(date_str)
    if not matchups_list:
        print("[!] 當日無賽事或無法取得賽事清單，程式結束。")
        return
        
    all_matchups_data = []
    
    # 2. 迴圈抓取每對賽事的 picks 頁面並進行解析
    for i, matchup in enumerate(matchups_list):
        print(f"\n[{i+1}/{len(matchups_list)}] 正在處理對戰組合...")
        
        matchup_data = parse_matchup_details(matchup)
        if not matchup_data:
            print(f"  [跳過] 無法抓取或解析該場對戰: {matchup['path']}")
            continue
            
        print(f"  對戰雙方: {matchup_data['team_a']} vs {matchup_data['team_b']}")
        print(f"  原始趨勢數: {len(matchup_data['trends'])} 條")
        
        # 3. 標準化分類趨勢
        processed_trends = classify_and_process_trends(matchup_data)
        
        # 4. 智能篩選媒合 (大小分與獨贏/讓分)
        double_pos, opposing = analyze_betting_recommendations(matchup_data, processed_trends)
        
        print(f"  -> 篩選出 [大小分總分]: {len(double_pos)} 項 | [勝負/讓分盤]: {len(opposing)} 盤口")
        
        all_matchups_data.append({
            'path': matchup_data['path'],
            'team_a': matchup_data['team_a'],
            'team_b': matchup_data['team_b'],
            'team_a_logo': matchup_data['team_a_logo'],
            'team_b_logo': matchup_data['team_b_logo'],
            'processed_trends': processed_trends,
            'double_positive': double_pos,
            'opposing_trends': opposing
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
                'recommendation': rec['recommendation'],
                'confidence': rec['confidence'],
                'roi': rec['avg_roi'],
                'logo_a': matchup['team_a_logo'],
                'logo_b': matchup['team_b_logo'],
                'details': f"雙方平均投報率: {rec['avg_roi']}%"
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
                'roi': rec['strong_roi'],
                'roi_diff': rec['roi_diff'],
                'bet_on': rec['bet_on'],
                'logo': logo_url,
                'details': f"優勢隊投報率: {rec['strong_roi']}% (雙方差距: {rec['roi_diff']}% ROI)"
            })
            
    # 分別對兩組推薦以投報率 ROI 由大到小排序，取各自的前 5 名 (Top 5)
    top_5_sides = sorted(sides_recs, key=lambda x: x['roi'], reverse=True)[:5]
    top_5_totals = sorted(totals_recs, key=lambda x: x['roi'], reverse=True)[:5]
    
    # 5.5 計算「今日 AI 推薦 Top 3」 (智慧過濾方向衝突，依 ROI 排序)
    conflicting_matchups = set()
    matchup_bet_teams = {}
    for r in sides_recs:
        m_id = r['matchup_id']
        team = r.get('bet_on')
        if m_id not in matchup_bet_teams:
            matchup_bet_teams[m_id] = set()
        matchup_bet_teams[m_id].add(team)
        
    for m_id, teams in matchup_bet_teams.items():
        if len(teams) > 1:
            conflicting_matchups.add(m_id)
            
    ai_candidates = []
    for r in sides_recs:
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
            'roi': r['roi'],
            'roi_diff': r['roi_diff'],
            'bet_on': r['bet_on'],
            'logo_a': r['logo'],
            'logo_b': None,
            'rationale': f"黃金對立組合！優勢隊 {r['bet_on']} 歷史投報率達 {r['roi']}%，且雙方 ROI 差距達 {r['roi_diff']}%，戰績勢力差距顯著。"
        })
        
    for r in totals_recs:
        ai_candidates.append({
            'matchup_id': r['matchup_id'],
            'matchup_name': r['matchup_name'],
            'type': 'double',
            'type_zh': '大小分總分',
            'market_type': r['market_type'],
            'recommendation': r['recommendation'],
            'confidence': r['confidence'],
            'roi': r['roi'],
            'logo_a': r['logo_a'],
            'logo_b': r['logo_b'],
            'rationale': f"雙向強勢指標！兩隊近期在 {r['market_type']} 盤口高度吻合，歷史平均投報率達 {r['roi']}%，大/小分走勢非常清晰。"
        })
        
    top_3_ai = sorted(ai_candidates, key=lambda x: x['roi'], reverse=True)[:3]
    
    print(f"\n[+] 成功計算出今日「勝負/讓分盤」與「大小分總分」雙欄 Top 5 黃金投注推薦。")
    print(f"[+] 成功計算出今日「AI 推薦 Top 3」精選：{len(top_3_ai)} 項組合。")
    
    # 6. 生成動態互動式 HTML 數據儀表板
    generate_html_dashboard(all_matchups_data, top_5_sides, top_5_totals, top_3_ai, date_str)
    
    print("====================================================")
    print("                  抓取與分析完成！")
    print("====================================================")

if __name__ == "__main__":
    main()
