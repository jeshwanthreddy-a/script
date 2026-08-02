import os
import json
import difflib
from datetime import datetime
from fastapi import FastAPI, Body, File, UploadFile
import pandas as pd
from groq import Groq

app = FastAPI()

# Initialize Groq Cloud Client using Environment Variable
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

INVENTORY_CSV = "business_inventory.csv"
LEDGER_CSV = "business_ledgers.csv"

# In-Memory Fallback Functions for Audit Tracking
def save_audit_entry(task_id, source_mode, narration, status):
    pass

def update_audit_entry(task_id, status=None, voucher_type=None, party_name=None, items=None, transcript=None, **kwargs):
    pass

@app.get("/")
def read_root():
    return {"status": "Backend Engine Running on Groq Cloud"}

def fuzzy_match_ledger(extracted_name):
    if not os.path.exists(LEDGER_CSV) or not extracted_name:
        return {"match": None, "suggested": False}
    df = pd.read_csv(LEDGER_CSV)
    df.columns = [c.strip() for c in df.columns]
    col = df.columns[0]
    names = df[col].dropna().astype(str).tolist()
    clean_target = str(extracted_name).strip()

    if clean_target.lower() in ["cash", "cash account", ""]:
        return {"match": "Cash", "suggested": False}
    for name in names:
        if clean_target.lower() == name.lower():
            return {"match": name, "suggested": False}
    matches = difflib.get_close_matches(clean_target, names, n=1, cutoff=0.5)
    return {"match": matches[0], "suggested": True} if matches else {"match": None, "suggested": False}

def fuzzy_match_item(keyword):
    if not os.path.exists(INVENTORY_CSV) or not keyword:
        return None
    df = pd.read_csv(INVENTORY_CSV)
    df.columns = [c.strip() for c in df.columns]
    name_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
    hsn_col = df.columns[2] if len(df.columns) > 2 else None

    items = df[name_col].dropna().astype(str).tolist()
    clean_kw = str(keyword).strip()

    for item in items:
        if clean_kw.lower() in item.lower() or item.lower() in clean_kw.lower():
            hsn = str(df[df[name_col] == item][hsn_col].values[0]) if hsn_col else "123499"
            return {"name": item, "hsn": hsn}
    matches = difflib.get_close_matches(clean_kw, items, n=1, cutoff=0.4)
    if matches:
        item = matches[0]
        hsn = str(df[df[name_col] == item][hsn_col].values[0]) if hsn_col else "123499"
        return {"name": item, "hsn": hsn}
    return None

def generate_tally_xml(voucher_type, party_name, items, assessable_value, cgst, sgst, total_value):
    date_str = datetime.now().strftime("%Y%m%d")
    xml = "<ENVELOPE>\n <HEADER>\n  <TALLYREQUEST>Import Data</TALLYREQUEST>\n </HEADER>\n <BODY>\n  <DATA>\n"
    xml += f'   <VOUCHER VCHTYPE="{voucher_type}" ACTION="Create" OBJVIEW="AccountingVoucherView">\n'
    xml += f"    <DATE>{date_str}</DATE>\n    <VOUCHERTYPENAME>{voucher_type}</VOUCHERTYPENAME>\n"
    xml += f"    <PARTYLEDGERNAME>{party_name}</PARTYLEDGERNAME>\n"
    xml += f"    <ALLLEDGERENTRIES.LIST>\n     <LEDGERNAME>{party_name}</LEDGERNAME>\n     <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>\n     <AMOUNT>-{total_value:.2f}</AMOUNT>\n    </ALLLEDGERENTRIES.LIST>\n"
    xml += f"    <ALLLEDGERENTRIES.LIST>\n     <LEDGERNAME>Sales Account</LEDGERNAME>\n     <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n     <AMOUNT>{assessable_value:.2f}</AMOUNT>\n    </ALLLEDGERENTRIES.LIST>\n"
    
    if cgst > 0:
        xml += f"    <ALLLEDGERENTRIES.LIST>\n     <LEDGERNAME>CGST @ 9%</LEDGERNAME>\n     <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n     <AMOUNT>{cgst:.2f}</AMOUNT>\n    </ALLLEDGERENTRIES.LIST>\n"
    if sgst > 0:
        xml += f"    <ALLLEDGERENTRIES.LIST>\n     <LEDGERNAME>SGST @ 9%</LEDGERNAME>\n     <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n     <AMOUNT>{sgst:.2f}</AMOUNT>\n    </ALLLEDGERENTRIES.LIST>\n"

    for itm in items:
        xml += "    <ALLINVENTORYENTRIES.LIST>\n"
        xml += f"     <STOCKITEMNAME>{itm['Item']}</STOCKITEMNAME>\n     <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n"
        xml += f"     <RATE>{itm['Rate']}/Nos</RATE>\n     <AMOUNT>{itm['Total']:.2f}</AMOUNT>\n"
        xml += f"     <ACTUALQTY> {itm['Qty']} Nos</ACTUALQTY>\n     <BILLEDQTY> {itm['Qty']} Nos</BILLEDQTY>\n"
        xml += f"     <HSNCODE>{itm.get('HSN', '')}</HSNCODE>\n    </ALLINVENTORYENTRIES.LIST>\n"

    xml += "   </VOUCHER>\n  </DATA>\n </BODY>\n</ENVELOPE>"
    return xml

@app.post("/v2/statement/parse")
def parse_statement(payload: dict = Body(...)):
    narration = payload.get("narration", "").strip()
    if not narration:
        return {"status": "FAILED", "error": "No narration provided"}

    if not client:
        return {"status": "FAILED", "error": "GROQ_API_KEY environment variable missing in Render"}

    system_prompt = (
        "Extract accounting entities into valid JSON. Rules: Default voucher_type to 'Sales'. "
        "Strict JSON schema: {\"voucher_type\": \"Sales\", \"party_name\": \"Name\", \"items\": [{\"item_keyword\": \"keyword\", \"quantity\": 1, \"rate\": 100}]}"
    )

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": narration}
            ],
            response_format={"type": "json_object"}
        )
        extracted_data = json.loads(response.choices[0].message.content)

        raw_party = extracted_data.get("party_name", "").strip()
        ledger_res = fuzzy_match_ledger(raw_party)
        verified_party = ledger_res["match"]

        if not verified_party:
            return {
                "status": "LEDGER_NOT_FOUND",
                "missing_party": raw_party if raw_party else "Unknown New Client",
                "transcript": narration
            }

        processed_items = []
        assessable_value = 0.0
        v_type = extracted_data.get("voucher_type", "Sales")

        for itm in extracted_data.get("items", []):
            kw = itm.get("item_keyword", "")
            item_match = fuzzy_match_item(kw)
            if not item_match:
                return {
                    "status": "ITEM_NOT_FOUND",
                    "missing_keyword": kw if kw else "Unknown Stock Item",
                    "transcript": narration
                }

            qty = float(itm.get("quantity", 1))
            rate = float(itm.get("rate", 0))
            line_total = round(qty * rate, 2)
            assessable_value += line_total
            processed_items.append({
                "Item": item_match["name"],
                "Qty": qty,
                "Rate": rate,
                "Total": line_total,
                "HSN": item_match["hsn"]
            })

        cgst = round(assessable_value * 0.09, 2)
        sgst = round(assessable_value * 0.09, 2)
        total_value = round(assessable_value + cgst + sgst, 2)

        generated_xml = generate_tally_xml(v_type, verified_party, processed_items, assessable_value, cgst, sgst, total_value)

        return {
            "status": "SUCCESS",
            "voucher_type": v_type,
            "party_name": verified_party,
            "items": processed_items,
            "assessable_value": assessable_value,
            "cgst": cgst,
            "sgst": sgst,
            "total_value": total_value,
            "transcript": narration,
            "xml_preview": generated_xml
        }

    except Exception as e:
        return {"status": "FAILED", "error": str(e)}

@app.post("/v2/audio/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    if not client:
        return {"status": "FAILED", "error": "GROQ_API_KEY missing"}
    
    try:
        file_bytes = await file.read()
        transcription = client.audio.transcriptions.create(
            file=(file.filename, file_bytes),
            model="whisper-large-v3",
            prompt="Indian business transaction narration with ledger party names and amounts."
        )
        return {"status": "SUCCESS", "transcript": transcription.text}
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}
