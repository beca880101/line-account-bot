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

# === 讀取與寫入邏輯 (修復時區) ===

def record_transaction(user_id, group_id, amount, memo, raw_text):
    """將交易寫入 Google Sheet (已修復時區)"""
    sheet = get_worksheet()
    if sheet:
        # 設置台灣標準時間 (UTC+8)
        tz_utc_8 = datetime.timezone(datetime.timedelta(hours=8))
        dt = datetime.datetime.now(tz_utc_8).strftime("%Y-%m-%d %H:%M:%S")
        
        # 寫入資料：時間, 使用者ID, 群組ID (私聊時為Private), 金額, 備註, 原始指令
        sheet.append_row([dt, user_id, group_id or "Private", amount, memo, raw_text])

def get_filtered_transactions(user_id=None, group_id=None, time_filter=None):
    """根據來源(user/group)和時間篩選交易紀錄"""
    sheet = get_worksheet()
    if not sheet: return []

    rows = sheet.get_all_records()
    filtered_list = []
    
    for row in reversed(rows): 
        r_time = str(row.get("時間", ""))
        # 這裡使用 get("群組ID", "Private") 確保舊資料或未填寫時，預設為 Private
        r_gid = str(row.get("群組ID", "") or "Private") 
        r_uid = str(row.get("使用者ID", ""))
        r_amt = row.get("金額", 0)
        r_memo = str(row.get("備註", ""))

        if time_filter and not r_time.startswith(time_filter):
            continue

        target = False
        # Group logic: Must match the group ID (only occurs if gid is not None)
        if group_id and r_gid == group_id:
            target = True
        # Private logic: Must match the user ID AND the group ID must be the private tag ("Private" or empty/default)
        elif user_id and r_uid == user_id and (r_gid == "Private" or r_gid == ""):
            # Note: The code always writes "Private" for private chat now, 
            # but we keep "" for backward compatibility with old data.
            target = True

        if target:
            try:
                # 確保金額是數字
                amount = float(r_amt)
            except (TypeError, ValueError):
                # 如果金額不是有效數字，跳過該行
                continue
                
            filtered_list.append({
                "time": r_time, 
                "amount": amount, 
                "memo": r_memo
            })
            
    return filtered_list

# === Flex Message 建立器 (顯示近 10 筆表格) (保持不變) ===

def build_recent_transactions_flex(records: list):
    """根據紀錄列表建立一個模擬表格的 Flex Message (Bubble Type)"""
    contents = []
    
    # Header Row
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
    
    # Data Rows
    for record in records:
        date_short = record["time"][5:10]
        amount_str = f"{record['amount']:,.0f}" 
        
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

# === 記帳成功確認 Flex Message (恢復成舊版詳細格式) ===

def build_transaction_confirm_flex(delta, memo, previous_bal, new_bal):
    """建立記帳成功後回覆的 Flex Message (含上次累積、本次交易、目前累積)"""
    
    delta_color = "#38761d" if delta >= 0 else "#cc0000"
    new_bal_color = "#1DB446" if new_bal >= 0 else "#cc0000"

    # 格式化金額
    format_amount = lambda x: f"{round(x, 2):,}"

    flex_content = BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(
                    text="記帳成功!",
                    weight='bold', size='xl', color='#1DB446'
                ),
                SeparatorComponent(margin='md'),
                
                # 備註
                BoxComponent(
                    layout='horizontal', margin='sm',
                    contents=[
                        TextComponent(text='備註：', size='sm', color='#555555', flex=2, weight='bold'),
                        TextComponent(text=memo, size='sm', color='#333333', flex=6, wrap=True, align='end')
                    ]
                ),
                SeparatorComponent(margin='lg', color='#CCCCCC'),
                
                # 上次累積
                BoxComponent(
                    layout='horizontal', margin='sm',
                    contents=[
                        TextComponent(text='上次累積：', size='md', color='#888888', flex=5),
                        TextComponent(text=f"{format_amount(previous_bal)} 元", size='md', color='#888888', flex=4, align='end', weight='bold')
                    ]
                ),
                # 本次交易
                BoxComponent(
                    layout='horizontal', margin='sm',
                    contents=[
                        TextComponent(text='本次交易：', size='md', color='#555555', flex=5),
                        TextComponent(text=f"{format_amount(delta)} 元", size='lg', color=delta_color, flex=4, align='end', weight='bold')
                    ]
                ),
                SeparatorComponent(margin='lg', color='#CCCCCC'),
                
                # 目前累積 (計算結果)
                BoxComponent(
                    layout='horizontal', margin='sm',
                    contents=[
                        TextComponent(text='目前累積：', size='lg', color='#333333', flex=5, weight='bold'),
                        TextComponent(text=f"{format_amount(new_bal)} 元", size='xl', color=new_bal_color, flex=4, align='end', weight='bold')
                    ]
                )
            ]
        )
    )
    return FlexSendMessage(alt_text="記帳成功確認", contents=flex_content)


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
    
    sheet = get_worksheet()
    if not sheet:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗，請檢查 Render Log 或環境變數設定！"))
        return

    # 指令：查 ID
    if text == "/id":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"你的 userId 是：\n{uid}\n群組 ID 是：\n{gid}"))
        return
        
    # 指令：報表 / Report
    if text.lower() in ["報表", "report", "excel"]:
        
        current_month = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m")
        all_month_records = get_filtered_transactions(user_id=uid, group_id=gid, time_filter=current_month)
        
        if not all_month_records:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="本月尚無紀錄！"))
            return
            
        monthly_total = sum(r['amount'] for r in all_month_records)
        recent_10_records = all_month_records[:10]
        
        flex_message = build_recent_transactions_flex(recent_10_records)
        sheet_url = "https://docs.google.com/spreadsheets/d/" + sheet.spreadsheet.id
        msg_summary = (
            f"💰 {current_month} 月總結\n"
            f"筆數：{len(all_month_records)} 筆\n"
            f"總累積：{round(monthly_total, 2)} 台幣\n\n"
            f"🔗 詳細 Excel 表格請點擊：\n{sheet_url}"
        )
        text_message = TextSendMessage(text=msg_summary)
        
        line_bot_api.reply_message(event.reply_token, [flex_message, text_message])
        return
        
    # 指令：餘額 (包含「小朋友欠」邏輯)
    if text in ["餘額", "balance"]:
        bal = sum(r['amount'] for r in get_filtered_transactions(user_id=uid, group_id=gid))
        rounded_bal = round(bal, 2)
        
        if rounded_bal > 0:
            msg_text = (
                f"📊 目前總累積：{rounded_bal:,.2f} 元\n"
                f"👉 依據慣例，目前小朋友欠 {abs(rounded_bal):,.2f} 元"
            )
        elif rounded_bal < 0:
            msg_text = (
                f"📊 目前總累積：{rounded_bal:,.2f} 元\n"
                f"👉 依據慣例，目前欠小朋友 {abs(rounded_bal):,.2f} 元"
            )
        else:
            msg_text = "目前總累積：0 元 (沒有積欠)"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg_text))
        return

    # 記帳邏輯
    try:
        delta, memo = parse_expr_and_memo(text)
        
        # 1. 計算上次累積 (在本次交易前)
        previous_bal = sum(r['amount'] for r in get_filtered_transactions(user_id=uid, group_id=gid))
        
        # 2. 計算本次累積
        new_bal = previous_bal + delta
        
        # 3. 寫入 Google Sheet (永久保存)
        record_transaction(uid, gid, delta, memo, text)
        
        # 4. 回覆：使用恢復後的 Flex Message
        flex_message = build_transaction_confirm_flex(delta, memo, previous_bal, new_bal)
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
