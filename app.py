# ================================================================
#  LINE 記帳機器人（Google Sheet + 報表 + 強健解析 + 防誤記）
# ================================================================

import os
import json
import datetime
import ast
import operator as op

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
from zoneinfo import ZoneInfo

# ================================================================
#  環境變數
# ================================================================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("缺少 LINE Token（LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN）")

if not GOOGLE_SHEET_NAME:
    raise ValueError("請在環境變數設定 GOOGLE_SHEET_NAME")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ================================================================
#  Google Sheet 連線
# ================================================================
def get_sheet():
    if not GOOGLE_CREDENTIALS_JSON:
        print("【錯誤】未設定 GOOGLE_CREDENTIALS_JSON")
        return None

    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)

        sheet = client.open(GOOGLE_SHEET_NAME).sheet1

        if not sheet.get_all_values():
            sheet.append_row(["時間", "使用者ID", "群組ID", "金額", "備註", "原始指令"])

        return sheet

    except Exception as e:
        print("【Google Sheets 連線錯誤】", e)
        return None


# ================================================================
#  全形 → 半形
# ================================================================
def to_halfwidth(s):
    result = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            result.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return "".join(result)


# ================================================================
#  安全運算
# ================================================================
allowed_ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

def safe_eval(expr):
    expr = expr.replace(" ", "")
    if not expr:
        raise ValueError("empty expr")

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Num):
            return node.n
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


# ================================================================
#  記帳解析
# ================================================================
def parse_transaction(text):
    original = text.strip()
    s = to_halfwidth(original)

    if not s or s[0] not in "+-":
        raise ValueError("not transaction")

    allowed_chars = set("0123456789+-*/(). ")

    expr_chars = []
    for ch in s:
        if ch in allowed_chars:
            expr_chars.append(ch)
        else:
            break

    expr_str = "".join(expr_chars).strip()

    if not expr_str or not any(c.isdigit() for c in expr_str):
        raise ValueError("no digits in expr")

    amount = safe_eval(expr_str)

    memo = s[len("".join(expr_chars)) :].strip()
    if not memo:
        memo = "無備註"

    return amount, memo, expr_str


# ================================================================
#  寫入 Google Sheet
# ================================================================
def write_record(user_id, group_id, amount, memo, raw_text):
    sheet = get_sheet()
    if not sheet:
        return False

    now = datetime.datetime.now(ZoneInfo("Asia/Taipei"))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    gid_to_save = group_id if group_id else "Private"

    sheet.append_row([now_str, user_id, gid_to_save, amount, memo, raw_text])
    return True


# ================================================================
#  查詢紀錄
# ================================================================
def get_transactions_for_context(sheet, user_id, group_id, year_month=None):
    rows = sheet.get_all_records()
    records = []

    for row in reversed(rows):
        r_time = str(row.get("時間", ""))
        r_uid = str(row.get("使用者ID", ""))
        r_gid = str(row.get("群組ID", ""))
        r_amt = row.get("金額", 0)
        r_memo = str(row.get("備註", ""))

        if year_month and not r_time.startswith(year_month):
            continue

        if group_id:
            if r_gid != group_id:
                continue
        else:
            if not (r_gid == "Private" and r_uid == user_id):
                continue

        try:
            amount = float(r_amt)
        except:
            continue

        try:
            dt = datetime.datetime.strptime(r_time, "%Y-%m-%d %H:%M:%S")
            display_time = dt.strftime("%m/%d %H:%M")
        except:
            display_time = r_time

        records.append(
            {"time": display_time, "amount": amount, "memo": r_memo}
        )

    return records


# ================================================================
#  計算餘額
# ================================================================
def calc_balance(user_id, group_id):
    sheet = get_sheet()
    if not sheet:
        return None

    rows = sheet.get_all_records()
    bal = 0.0

    for row in rows:
        r_uid = str(row.get("使用者ID", ""))
        r_gid = str(row.get("群組ID", ""))
        r_amt = row.get("金額", 0)

        try:
            amt = float(r_amt)
        except:
            continue

        if group_id:
            if r_gid == group_id:
                bal += amt
        else:
            if r_gid == "Private" and r_uid == user_id:
                bal += amt

    return bal


# ================================================================
#  Flex：記帳成功卡片
# ================================================================
def build_settle_flex(prev_amount, delta, total, unit="台幣", current_label="目前欠款", memo=None):
    prev_amount = round(prev_amount, 2)
    delta = round(delta, 2)
    total = round(total, 2)

    sign = "+" if delta >= 0 else "-"
    delta_abs = abs(delta)

    memo_text = f"備註：{memo}" if memo else "備註："

    return FlexSendMessage(
        alt_text="計算結果",
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": "計算結果", "weight": "bold", "size": "lg", "color": "#2E7D32"},
                    {"type": "text", "text": f"{sign}{delta_abs} = {total}", "size": "sm", "color": "#8D6E63", "align": "end"},
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "上次金額", "size": "sm"},
                                    {"type": "text", "text": f"{prev_amount} {unit}", "size": "sm", "align": "end", "color": "#8D6E63"},
                                ],
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "本次金額", "size": "sm"},
                                    {"type": "text", "text": f"{delta} {unit}", "size": "sm", "align": "end", "color": "#8D6E63"},
                                ],
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": current_label, "size": "sm"},
                                    {"type": "text", "text": f"{total} {unit}", "size": "sm", "align": "end", "color": "#8D6E63"},
                                ],
                            },
                        ],
                    },
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": memo_text, "size": "xs", "color": "#B0BEC5", "wrap": True},
                ],
            },
        },
    )


# ================================================================
#  Flex：近 10 筆報表
# ================================================================
def build_report_flex(records, month_label, monthly_total):
    total_str = f"{monthly_total:.2f}".rstrip("0").rstrip(".")

    rows_contents = []
    for r in records:
        amt = r["amount"]
        memo = r["memo"]
        time_str = r["time"]

        sign = "+" if amt >= 0 else "-"
        amt_abs_str = f"{abs(amt):.2f}".rstrip("0").rstrip(".")

        row = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{sign}{amt_abs_str}", "size": "sm", "flex": 2},
                {"type": "text", "text": memo, "size": "sm", "flex": 5, "wrap": True},
                {"type": "text", "text": time_str, "size": "xs", "flex": 3, "align": "end", "color": "#888888"},
            ],
        }

        rows_contents.append(row)

    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "📘 近 10 筆記帳紀錄", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"{month_label} 本月", "size": "sm", "color": "#666666"},
                {"type": "separator", "margin": "md"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "contents": rows_contents},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"本月累積：{total_str} 元", "size": "sm", "weight": "bold"},
            ],
        },
    }

    return FlexSendMessage(alt_text="本月記帳報表", contents=bubble)


# ================================================================
#  LINE Webhook
# ================================================================
@app.route("/callback", methods=["POST", "HEAD"])
def callback():
    if request.method == "HEAD":
        return ("", 200)

    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ================================================================
#  訊息處理
# ================================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    group_id = event.source.group_id if event.source.type == "group" else None

    # -------- 餘額 --------
    if text in ["餘額", "balance"]:
        bal = calc_balance(user_id, group_id)
        if bal is None:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗 🥲"))
            return

        bal_r = round(bal, 2)
        if bal_r > 0:
            msg = f"目前小朋友欠 {bal_r} 元"
        elif bal_r < 0:
            msg = f"目前欠小朋友 {abs(bal_r)} 元"
        else:
            msg = "目前互不相欠 ✨"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # -------- 報表 --------
    cmd = text.lower()
    if cmd in ["報表", "report", "excel"]:
        sheet = get_sheet()
        if not sheet:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗 🥲"))
            return

        month = datetime.datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m")
        all_records = get_transactions_for_context(sheet, user_id, group_id, year_month=month)

        if not all_records:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{month} 本月尚無紀錄"))
            return

        monthly_total = sum(r["amount"] for r in all_records)
        recent_10 = all_records[:10]

        flex = build_report_flex(recent_10, month, monthly_total)
        sheet_url = "https://docs.google.com/spreadsheets/d/" + sheet.spreadsheet.id

        summary = (
            f"📘 {month} 本月總結\n"
            f"筆數：{len(all_records)} 筆\n"
            f"總累積：{round(monthly_total, 2)} 元\n\n"
            f"🔗 完整紀錄：{sheet_url}"
        )

        line_bot_api.reply_message(event.reply_token, [flex, TextSendMessage(text=summary)])
        return

    # -------- 記帳 --------
    try:
        amount, memo, expr_str = parse_transaction(text)

        ok = write_record(user_id, group_id, amount, memo, text)
        if not ok:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Google Sheets 連線失敗 🥲"))
            return

        bal = calc_balance(user_id, group_id)
        prev_amount = bal - amount  # ⭐ 正確：上次餘額

        flex = build_settle_flex(prev_amount, amount, bal, memo=memo)
        line_bot_api.reply_message(event.reply_token, flex)
        return

    except ValueError:
        return  # 不是記帳 → 忽略
    except Exception as e:
        print("【處理訊息錯誤】", e)
        return


# ================================================================
#  本地啟動（Render 用 gunicorn）
# ================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
