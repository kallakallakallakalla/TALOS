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
})
open(os.environ["PICKUP_ZONES"], "w").write("zone,area,airport\n50,Cologne,CGN\n51,Bergisch Gladbach,CGN\n60,Frankfurt am Main,FRA\n")
open(os.environ["CARRIER_LIST"], "w").write(
    "name,kind,email,serves\nLH Desk,airline,lh@air.test,ORD\n"
    "Truck One,trucker,t1@trucks.test,50 51\nTruck Two,trucker,t2@trucks.test,51 60\n"
    "Truck Three,trucker,t3@trucks.test,51\nTruck Four,trucker,t4@trucks.test,60\n")
from quote_bot.main import main

mode = sys.argv[1] if len(sys.argv) > 1 else "once"
if mode == "dump":
    main(["--dump", "--dump-chars", "400"])
else:
    main(["--once", "--reply"])
    print("\n--- second run: booking ---")
    main(["--once", "--reply"])
    print("\n--- third run: tracking ---")
    main(["--once", "--reply"])
    import sqlite3
    c = sqlite3.connect(os.environ["DB_PATH"])
    print("messages:", c.execute("select id, category, method, status, llm_tokens from messages").fetchall())
    print("bookings:", c.execute("select rfq_id, booking_reference, team_reference, price, currency, status from bookings").fetchall())
    print("quotes:", c.execute("select message_id, lot, airline, total, currency, quoted_weight_kg, break_applied from quotes").fetchall())
    print("since_id:", c.execute("select value from state").fetchall())
    sent = mock_api.calls.pop("sent", [])
    print("mock calls:", mock_api.calls)
    for m in sent:
        print("\n=== SENT ===", {k: v for k, v in m.items() if k != "body"})
        print(m["body"])
