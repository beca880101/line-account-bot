import os
import ast
import json
import datetime
import operator as op
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, BubbleContainer, BoxComponent, TextComponent, SeparatorComponent

# ===== 環境變數讀取與設定 =====
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Line記帳本")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("請先設定 LINE Token")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# === Google Sheet 連線設定 ===
def get_worksheet():
    """連線到 Google Sheet 並取得工作表物件"""
    if not GOOGLE_CREDENTIALS_JSON:
        print("錯誤：未設定 GOOGLE_CREDENTIALS_JSON")
        return None
    
    try:
        # 嘗試解析 JSON 金鑰
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # 嘗試打開試算表
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        
        # 初始化標題列（如果工作表為空）
        if not sheet.get_all_values():
            sheet.append_row(["時間", "使用者ID", "群組ID", "金額", "備註", "原始指令"])
            
        return sheet
    
    except json.JSONDecodeError as e:
        print(f"致命錯誤：GOOGLE_CREDENTIALS_JSON 格式錯誤 (請確保是單行文字): {e}") 
        # 由於這是致命錯誤，回傳 None 並讓錯誤在 handle_message 中處理
        return None
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"致命錯誤：找不到試算表，請檢查名稱是否正確: {GOOGLE_SHEET_NAME}")
        return None
    except Exception as e:
        print(f"Google Sheet 連線時發生未預期錯誤: {e}")
        return None

# === 數學運算邏輯 ===
allowed_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

def safe_eval_expr(expr: str) -> float:
    """安全地解析並計算數學運算式"""
    expr = expr.replace(" ", "")
    if not expr: raise ValueError("empty expression")
    def _eval(node):
        if isinstance(node, ast.Num): return node.n
        if isinstance(node, ast.BinOp):
            if type(node.op) not in allowed_ops: raise ValueError("bad op")
            return allowed_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in allowed_unary: raise ValueError("bad unary")
            return allowed_unary[type(node.op)](_eval(node.operand))
        raise ValueError("bad expr")
    tree = ast.parse(expr, mode="eval")
    return float(_eval(tree.body))

def parse_expr_and_memo(raw: str):
    """從原始文字中解析出金額和備註"""
    s = raw.strip()
    if not s or s[0] not in "+-": raise ValueError("no leading sign")
    allowed_chars = set("0123456789.+-*/()")
    expr_chars = []
    i = 0
    for i, ch in enumerate(s):
        if ch in allowed_chars: expr_chars.append(ch)
        else: break
    expr = "".join(expr_chars).strip()
    if not expr or not any(c.isdigit() for c in expr): raise ValueError("no numeric expr")
    memo = s[len(expr):].strip()
    delta = safe_eval_expr(expr)
    return delta, memo or "無備註"

# === 讀取與寫入邏輯 ===

def record_transaction(user_id, group_id, amount, memo, raw_text):
    """將交易寫入 Google Sheet"""
    sheet = get_worksheet()
    if sheet:
        dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 寫入資料：時間, 使用者ID, 群組ID (私聊時為Private), 金額, 備註, 原始指令
        sheet.append_row([dt, user_id, group_id or "Private", amount, memo, raw_text])

def get_filtered_transactions(user_id=None, group_id=None, time_filter=None):
    """根據來源(user/group)和時間篩選交易紀錄"""
    sheet = get_worksheet()
    if not sheet: return []

    rows = sheet.get_all_records()
    filtered_list = []
    
    # 從最新的一筆開始篩選 (假設資料是按時間順序寫入)
    for row in reversed(rows): 
        r_time = str(row.get("時間", ""))
        r_gid = str(row.get("群組ID", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_amt = row.get("金額", 0)
        r_memo = str(row.get("備註", ""))

        # 1. 時間篩選 (例如: 2025-11)
        if time_filter and not r_time.startswith(time_filter):
            continue

        # 2. 來源篩選
        target = False
        if group_id and r_gid == group_id:
            target = True
        elif user_id and r_uid == user_id and (r_gid == "Private" or r_gid == ""):
            target = True
            
        if target:
            filtered_list.append({
                "time": r_time, 
                "amount": float(r_amt), 
                "memo": r_memo
            })
            
    # filtered_list 已經是最新在前的順序
    return filtered_list

# === Flex Message 建立器 (顯示近 10 筆表格) ===

def build_recent_transactions_flex(records: list):
    """根據紀錄列表建立一個模擬表格的 Flex Message (Bubble Type)"""
    contents = []
    
    # 1. Header Row
    header = BoxComponent(
        layout='horizontal', spacing='sm', margin='sm',
        contents=[
            TextComponent(text="日期", size='sm', flex=3, color='#7B1FA2', weight='bold'),
            TextComponent(text="金額", size='sm', flex=2, align='end', color='#7B1FA2', weight='bold'),
            TextComponent(text="備註", size='sm', flex=5, color='#7B1FA2', wrap=True, weight='bold'),
        ]
    )
    contents.append(header)
    contents.append(SeparatorComponent(margin='xs'))
    
    # 2. Data Rows
    for record in records:
        date_short = record["time"][5:10] # 擷取 MM-DD 格式
        amount_str = f"{record['amount']:,.0f}" # 格式化金額
        
        row = BoxComponent(
            layout='horizontal', spacing='sm', margin='xs',
            contents=[
                TextComponent(text=date_short, size='xs', flex=3),
                TextComponent(text=amount_str, size='xs', flex=2, align='end', color='#1A1A1A'),
                TextComponent(text=record["memo"], size='xs', flex=5, wrap=True),
            ]
        )
        contents.append(row)
        
    # 建立 Bubble 框架
    flex_content = BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(
                    text="📅 最近記帳 (Max 10 筆)",
                    weight='bold', size='md', color='#7B1FA2'
                ),
                SeparatorComponent(margin='md'),
                BoxComponent(
                    layout='vertical',
                    contents=contents,
                    spacing='none', padding_all='none'
                )
            ]
        )
    )
    return FlexSendMessage(alt_text="最近記帳紀錄", contents=flex_content)

# === LINE Bot 處理 ===

@app.route("/callback", methods=['POST'])
def callback():
    if request.method == 'HEAD':
        return ('', 200)
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    uid = event.source.user_id
    # 在群組時為 group_id，私聊時為 None
    gid = event.source.group_id if event.source.type == "group" else None 
    
    # 取得 Google Sheet 物件，並處理連線失敗的狀況
    sheet = get_worksheet()
    if not sheet:
        # 如果連線失敗，回覆錯誤訊息
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗，請檢查 Render Log 或環境變數設定！"))
        return

    # 指令：查 ID
    if text == "/id":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"你的 userId 是：\n{uid}\n群組 ID 是：\n{gid}"))
        return
        
    # 指令：報表 / Report
    if text.lower() in ["報表", "report", "excel"]:
        
        current_month = datetime.datetime.now().strftime("%Y-%m")
        # 取得本月所有紀錄 (最新在最前)
        all_month_records = get_filtered_transactions(user_id=uid, group_id=gid, time_filter=current_month)
        
        if not all_month_records:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="本月尚無紀錄！"))
            return
            
        # 1. 總計月金額
        monthly_total = sum(r['amount'] for r in all_month_records)
        
        # 2. 取得最近 10 筆 (直接取前 10 個)
        recent_10_records = all_month_records[:10]
        
        # --- 建立回覆訊息 ---
        
        # 訊息 1: 近 10 筆表格 (Flex Message)
        flex_message = build_recent_transactions_flex(recent_10_records)
        
        # 訊息 2: 月總結和連結 (Text Message)
        sheet_url = "https://docs.google.com/spreadsheets/d/" + sheet.spreadsheet.id
        msg_summary = (
            f"💰 {current_month} 月總結\n"
            f"筆數：{len(all_month_records)} 筆\n"
            f"總累積：{round(monthly_total, 2)} 台幣\n\n"
            f"🔗 詳細 Excel 表格請點擊：\n{sheet_url}"
        )
        text_message = TextSendMessage(text=msg_summary)
        
        # 發送多個訊息
        line_bot_api.reply_message(event.reply_token, [flex_message, text_message])
        return
        
    # 指令：餘額 (包含「小朋友欠」邏輯)
    if text in ["餘額", "balance"]:
        # 取得目前累積總額
        bal = sum(r['amount'] for r in get_filtered_transactions(user_id=uid, group_id=gid))
        
        # 將總額取到小數點第二位
        rounded_bal = round(bal, 2)
        
        if rounded_bal > 0:
            # 正數 -> 小朋友欠錢
            # 使用 abs() 確保顯示的是正數金額
            msg_text = (
                f"📊 目前總累積：{rounded_bal} 元\n"
                f"👉 依據慣例，目前小朋友欠 {abs(rounded_bal)} 元"
            )
        elif rounded_bal < 0:
            # 負數 -> 欠小朋友錢
            msg_text = (
                f"📊 目前總累積：{rounded_bal} 元\n"
                f"👉 依據慣例，目前欠小朋友 {abs(rounded_bal)} 元"
            )
        else:
            msg_text = "目前總累積：0 元 (沒有積欠)"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg_text))
        return

    # 記帳邏輯
    try:
        delta, memo = parse_expr_and_memo(text)
        
        # 1. 寫入 Google Sheet (永久保存)
        record_transaction(uid, gid, delta, memo, text)
        
        # 2. 重新計算總額
        new_bal = sum(r['amount'] for r in get_filtered_transactions(user_id=uid, group_id=gid))
        
        # 3. 回覆
        msg_text = f"✅ 已記錄：{delta}\n備註：{memo}\n目前累積：{round(new_bal, 2)} 台幣"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg_text))

    except ValueError:
        # 非記帳指令，且非特殊指令，則回覆說明
        if text.lower() in ["說明", "help", "指令", "使用說明"]:
             help_text = (
                "💰 記帳機器人使用說明：\n"
                "1. 記帳：+金額備註 或 -金額備註，例如：+200午餐\n"
                "2. 報表：輸入 **報表** 取得本月總結和近 10 筆表格\n"
                "3. 餘額：輸入 **餘額** 查詢目前累積和積欠狀況\n"
            )
             line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
        pass # 其他無法解析的文字訊息不回覆

    except Exception as e:
        print(f"處理訊息時發生未預期錯誤: {e}")
        # 這裡不回覆給用戶，避免洩露內部錯誤細節
        pass


# 部署入口點
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
