import os
import ast
import json
import datetime
import operator as op
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

# ===== Google Sheets 相關套件 =====
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ===== 從環境變數讀取設定 =====
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
# 你的 Google Sheet 名稱 (請確保機器人帳號有權限編輯)
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Line記帳本")
# 將 credentials.json 的內容整串貼到 Render 環境變數 GOOGLE_CREDENTIALS_JSON 中
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("請先設定 LINE Token")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# === Google Sheet 連線設定 ===
def get_worksheet():
    if not GOOGLE_CREDENTIALS_JSON:
        print("錯誤：未設定 GOOGLE_CREDENTIALS_JSON")
        return None
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    # 開啟試算表，如果沒有 worksheet 則使用第一個
    sheet = client.open(GOOGLE_SHEET_NAME).sheet1
    
    # 初始化標題列 (如果是空的)
    if not sheet.get_all_values():
        sheet.append_row(["時間", "使用者ID", "群組ID", "金額", "備註", "原始指令"])
        
    return sheet

# === 數學運算邏輯 (保持原本優良的設計) ===
allowed_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

def safe_eval_expr(expr: str) -> float:
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
    s = raw.strip()
    if not s or s[0] not in "+-": raise ValueError("no leading sign")
    allowed_chars = set("0123456789.+-*/()")
    expr_chars = []
    for ch in s:
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
        # 欄位：時間, UserID, GroupID, 金額, 備註, 原始文字
        sheet.append_row([dt, user_id, group_id or "Private", amount, memo, raw_text])

def calculate_balance(user_id=None, group_id=None):
    """從 Sheet 讀取並計算總餘額"""
    sheet = get_worksheet()
    if not sheet: return 0.0
    
    rows = sheet.get_all_records() # 讀取所有資料為 List of Dict
    total = 0.0
    
    for row in rows:
        # 根據是群組還是個人來篩選
        r_gid = str(row.get("群組ID", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_amt = row.get("金額", 0)
        
        if group_id:
            if r_gid == group_id:
                total += float(r_amt)
        elif user_id:
            # 個人模式：只算沒有 Group ID 且 User ID 符合的
            if r_uid == user_id and (r_gid == "Private" or r_gid == ""):
                total += float(r_amt)
                
    return total

def generate_monthly_report(user_id=None, group_id=None):
    """產生本月報表與試算表連結"""
    sheet = get_worksheet()
    if not sheet: return "無法連結資料庫"

    rows = sheet.get_all_records()
    current_month = datetime.datetime.now().strftime("%Y-%m")
    
    monthly_total = 0.0
    count = 0
    
    # 篩選本月資料
    for row in rows:
        r_time = str(row.get("時間", ""))
        r_gid = str(row.get("群組ID", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_amt = float(row.get("金額", 0))
        
        if not r_time.startswith(current_month):
            continue
            
        target = False
        if group_id and r_gid == group_id:
            target = True
        elif user_id and r_uid == user_id and (r_gid == "Private" or r_gid == ""):
            target = True
            
        if target:
            monthly_total += r_amt
            count += 1
            
    # Google Sheet 的公開連結 (請自行在 Sheet 設定 共用->取得連結)
    # 這裡可以透過 API 取得，或是你直接把連結放在環境變數更好
    sheet_url = "https://docs.google.com/spreadsheets/d/" + sheet.spreadsheet.id
    
    return f"📅 {current_month} 月報表\n筆數：{count} 筆\n總金額：{round(monthly_total, 2)}\n\n📊 詳細 Excel 表格請看：\n{sheet_url}"

# === LINE Bot 處理 ===

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/")
def index():
    return "Line Bot with Google Sheets is running."

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    uid = event.source.user_id
    gid = event.source.group_id if event.source.type == "group" else None
    
    # 指令：報表 / Report (你的第2個需求)
    if text.lower() in ["報表", "report", "excel"]:
        msg = generate_monthly_report(user_id=uid, group_id=gid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 指令：餘額
    if text in ["餘額", "balance"]:
        bal = calculate_balance(user_id=uid, group_id=gid)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"目前總累積：{round(bal, 2)}"))
        return

    # 記帳邏輯
    try:
        delta, memo = parse_expr_and_memo(text)
        
        # 1. 寫入 Google Sheet (永久保存)
        record_transaction(uid, gid, delta, memo, text)
        
        # 2. 重新計算總額
        new_bal = calculate_balance(uid, gid)
        
        # 3. 回覆 Flex Message
        msg_text = f"已記錄：{delta}\n備註：{memo}\n目前累積：{round(new_bal, 2)}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg_text))
        
    except ValueError:
        # 不是記帳指令，直接忽略
        pass
    except Exception as e:
        print(f"Error: {e}")
        # 除錯用，正式上線建議拿掉
        # line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統發生錯誤"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
