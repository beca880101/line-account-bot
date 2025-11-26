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
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    FlexSendMessage,
)

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
    ...
    #（這裡維持你原本的程式就好）
    ...

# === 數學運算邏輯 ===
allowed_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

def safe_eval_expr(expr: str) -> float:
    expr = expr.replace(" ", "")
    if not expr:
        raise ValueError("empty expression")

    def _eval(node):
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            if type(node.op) not in allowed_ops:
                raise ValueError("bad op")
            return allowed_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in allowed_unary:
                raise ValueError("bad unary")
            return allowed_unary[type(node.op)](_eval(node.operand))
        raise ValueError("bad expr")

    tree = ast.parse(expr, mode="eval")
    return float(_eval(tree.body))

# === LINE Bot 處理 ===

@app.route("/callback", methods=['POST', 'HEAD'])
def callback():
    if request.method == 'HEAD':
        # UptimeRobot / 健康檢查用
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
