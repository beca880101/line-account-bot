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
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, BubbleContainer, BoxComponent, TextComponent, SeparatorComponent, SpacerComponent

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
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        
        # 初始化標題列（如果工作表為空）
        if not sheet.get_all_values():
            sheet.append_row(["時間", "使用者ID", "群組ID", "金額", "備註", "原始指令"])
            
        return sheet
    
    except json.JSONDecodeError as e:
        print(f"致命錯誤：GOOGLE_CREDENTIALS_JSON 格式錯誤 (請確保是單行文字): {e}") 
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

# === 讀取與寫入邏輯 (資料分組的核心) ===

def record_transaction(user_id, group_id, amount, memo, raw_text):
    """將交易寫入 Google Sheet"""
    sheet = get_worksheet()
    if sheet:
        dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 群組ID：私聊時存為 "Private"，群組/房間時存為其 ID
        group_id_to_save = group_id or "Private"
        sheet.append_row([dt, user_id, group_id_to_save, amount, memo, raw_text])

def get_filtered_transactions(user_id=None, group_id=None, time_filter=None):
    """
    根據來源(user/group)和時間篩選交易紀錄，嚴格分離個人帳本和群組帳本。
    
    - Private Chat (group_id is None): 只匹配 GID='Private' 或 GID='' 且 UID 匹配的紀錄。
    - Group Chat (group_id is not None): 只匹配 GID 等於 group_id 的紀錄。
    """
    sheet = get_worksheet()
    if not sheet: return []

    rows = sheet.get_all_records()
    filtered_list = []
    
    # 遍歷所有紀錄，從最新的一筆開始
    for row in reversed(rows): 
        r_time = str(row.get("時間", ""))
        r_gid = str(row.get("群組ID", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_amt = row.get("金額", 0)
        # r_memo = str(row.get("備註", ""))

        if time_filter and not r_time.startswith(time_filter):
            continue

        target = False
        
        # 1. Group/Room Chat 邏輯: 僅當傳入 group_id 且紀錄的群組 ID 嚴格匹配時才計入
        if group_id and r_gid == group_id:
            target = True
            
        # 2. Private Chat 邏輯: 僅當沒有傳入 group_id (私聊) 且紀錄的群組 ID 是 'Private' 或空 (舊資料) 且 UID 匹配時才計入
        elif group_id is None and r_uid == user_id and r_gid in ("Private", ""):
            target = True
            
        if target:
            try:
                # 只保留需要的欄位
                filtered_list.append({
                    "time": r_time, 
                    "amount": float(r_amt), 
                    "memo": str(row.get("備註", ""))
                })
            except ValueError:
                continue
            
    return filtered_list

# === Flex Message 建立器 (使用使用者提供的 JSON 結構) ===

def build_settle_flex(
    prev_amount: float,
    delta: float,
    total: float,
    unit: str = "台幣",
    current_label: str = "目前餘額",
    memo: str | None = None
):
    """結算結果小卡片 (使用使用者提供的 JSON 結構)"""
    # 數值取到小數點第二位
    prev_amount = round(prev_amount, 2)
    delta = round(delta, 2)
    total = round(total, 2)

    # 處理計算式和本次金額顯示
    # delta_abs 確保本次金額總是正數，但計算式需要顯示正確的正負號
    sign = "+" if delta >= 0 else "" 
    delta_abs = abs(delta)

    # 備註文字，如果沒有備註則顯示 "備註："
    memo_text = f"備註：{memo}" if memo else "備註："
    memo_display = memo_text
    
    # 確保傳入的 JSON 結構是合法的
    flex_content = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "計算結果",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#2E7D32" # 計算結果標題用綠色
                },
                {
                    "type": "text",
                    # 計算式: +100.0 = 100.0 (如果 delta 是 -100, 則為 -100.0 = 0.0)
                    "text": f"{sign}{delta:.1f} = {total:.1f}", 
                    "size": "sm",
                    "color": "#8D6E63",
                    "align": "end"
                },
                {
                    "type": "separator",
                    "margin": "md"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "spacing": "sm",
                    "contents": [
                        { # 上次金額
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": "上次金額", "size": "sm"},
                                {
                                    "type": "text",
                                    "text": f"{prev_amount:.1f} {unit}",
                                    "size": "sm",
                                    "align": "end",
                                    "color": "#8D6E63"
                                }
                            ]
                        },
                        { # 本次金額
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": "本次金額", "size": "sm"},
                                {
                                    "type": "text",
                                    # 本次金額顯示其絕對值，不帶正負號
                                    "text": f"{delta_abs:.1f} {unit}", 
                                    "size": "sm",
                                    "align": "end",
                                    "color": "#8D6E63"
                                }
                            ]
                        },
                        { # 目前餘額/欠款
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": current_label, "size": "sm", "weight": "bold"},
                                {
                                    "type": "text",
                                    # 總額顯示其絕對值
                                    "text": f"{abs(total):.1f} {unit}", 
                                    "size": "sm",
                                    "align": "end",
                                    "color": "#8D6E63",
                                    "weight": "bold"
                                }
                            ]
                        }
                    ]
                },
                {
                    "type": "separator",
                    "margin": "md"
                },
                { # 備註
                    "type": "text",
                    "text": memo_display,
                    "size": "xs",
                    "color": "#B0BEC5",
                    "wrap": True
                }
            ]
        }
    }
    
    # 使用 LineBot 的 FlexSendMessage 類別，傳入字典內容
    return FlexSendMessage(
        alt_text="計算結果",
        contents=flex_content
    )


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
    # 檢查是否為群組或房間，決定 group_id 是否為 None
    is_group = event.source.type in ("group", "room")
    gid = event.source.group_id if is_group else None 
    
    sheet = get_worksheet()
    if not sheet:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗，請檢查 Render Log 或環境變數設定！"))
        return

    # ... (其他指令處理保持不變，略)

    # 記帳邏輯
    try:
        delta, memo = parse_expr_and_memo(text)
        
        # 1. 寫入 Google Sheet (永久保存)
        # 如果是私聊，gid 會是 None，record_transaction 內會存為 "Private"
        record_transaction(uid, gid, delta, memo, text)
        
        # 2. 重新計算總額
        # 傳入 gid (群組 ID 或 None) 確保只篩選出當前聊天室的交易紀錄
        all_transactions = get_filtered_transactions(user_id=uid, group_id=gid)
        new_bal = sum(r['amount'] for r in all_transactions)

        # 3. 計算上次餘額：本次累積 - 本次交易
        prev_bal = new_bal - delta 
        
        # 4. 決定 current_label (餘額/欠小朋友)
        current_label = "目前餘額"
        if is_group:
            if new_bal > 0:
                current_label = "目前小朋友欠"
            elif new_bal < 0:
                current_label = "目前欠小朋友"
            else:
                current_label = "目前餘額"

        # 5. 回覆：使用 Flex Message，傳入所有數據
        # 如果 memo 是 "無備註" 則傳遞 None 給 build_settle_flex
        memo_to_pass = None if memo == "無備註" else memo
        
        flex_message = build_settle_flex(
            prev_amount=prev_bal, 
            delta=delta, 
            total=new_bal, 
            current_label=current_label, 
            memo=memo_to_pass
        )
        line_bot_api.reply_message(event.reply_token, flex_message)

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
        pass

    except Exception as e:
        print(f"處理訊息時發生未預期錯誤: {e}")
        pass


# 部署入口點
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
