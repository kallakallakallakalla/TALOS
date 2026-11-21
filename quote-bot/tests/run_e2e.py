import os
import sys
import tempfile

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import mock_api

srv = mock_api.start()
tmp = tempfile.mkdtemp()
os.environ.update({
    "FIVEU_API_KEY": "test-key",
    "FIVEU_BASE_URL": "http://127.0.0.1:8765",
    "DB_PATH": os.path.join(tmp, "q.sqlite3"),
    "LLM_MODEL": "gpt-5-nano",  # not offered -> should fall back to gpt-4.1-nano
    "PICKUP_ZONES": os.path.join(tmp, "zones.csv"),
    "CARRIER_LIST": os.path.join(tmp, "carriers.csv"),
    "PICKUP_WAIT_S": "20",
    "MAIL_LOG_DIR": os.path.join(tmp, "mail_logs"),
    "TARIFF_SEED_CSV": "",
    "HUMANIZE": "0",
})
open(os.environ["PICKUP_ZONES"], "w").write("zone,area,airport\n50,Cologne,CGN\n51,Bergisch Gladbach,CGN\n60,Frankfurt am Main,FRA\n")
open(os.environ["CARRIER_LIST"], "w").write(
    "name,kind,email,serves\nLufthansa Cargo,airline,lh@air.test,ORD JFK\nOrion Cargo Air,airline,orion@air.test,DXB\n"
    "Truck One,trucker,t1@trucks.test,50 51\nTruck Two,trucker,t2@trucks.test,51 60\n"
    "Truck Three,trucker,t3@trucks.test,51\nTruck Four,trucker,t4@trucks.test,60\n")
from quote_bot.main import main

mode = sys.argv[1] if len(sys.argv) > 1 else "once"
if mode == "dump":
    main(["--dump", "--dump-chars", "400"])
else:
    import sqlite3
    main(["--once", "--reply"])
    # simulate the old build: Lars' "please book" was marked done without booking
    acc = next(m for m in mock_api.MAILS if m["from"].startswith("Lars") and "accept" in m["body"])
    c = sqlite3.connect(os.environ["DB_PATH"])
    c.execute("INSERT OR REPLACE INTO messages(id, sender, subject, category, method, status, flags)"
              " VALUES(?,?,?,?,?,?,?)", (acc["id"], acc["from"], acc["subject"], "booking", "rules", "done", "[]"))
    c.commit()
    for i in range(6):
        print(f"\n--- run {i + 2} ---")
        main(["--once", "--reply"] + (["--redo-bookings"] if i == 0 else []))
    print("messages:", c.execute("select id, category, method, status, llm_tokens from messages").fetchall())
    print("tariffs stored:", c.execute("select count(*) from tariffs").fetchone()[0])
    print("klaus quote in_reply_to:", mock_api.FLOW.get("klaus_quoted_in_reply_to"))
    print("bookings:", c.execute("select rfq_id, booking_reference, team_reference, rate_id, price, currency, status from bookings").fetchall())
    print("quotes:", c.execute("select message_id, lot, airline, total, currency, quoted_weight_kg, break_applied from quotes").fetchall())
    sent = mock_api.calls.pop("sent", [])
    print("mock calls:", mock_api.calls, "| booked:", list(mock_api.BOOKED))
    for m in sent:
        print("\n=== SENT ===", {k: v for k, v in m.items() if k != "body"})
        print(m["body"])
