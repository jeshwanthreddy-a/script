import os
import json
from datetime import datetime
import difflib
import pandas as pd
import ollama
from celery import Celery
from faster_whisper import WhisperModel
from database import save_audit_entry, update_audit_entry

BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_engine = Celery("voicetotally_workers", broker=BROKER_URL, backend=BROKER_URL)

INVENTORY_CSV = "business_inventory.csv"
LEDGER_CSV = "business_ledgers.csv"
stt_pipeline = None

def get_stt_model():
    global stt_pipeline
    if stt_pipeline is None:
        stt_pipeline = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)
    return stt_pipeline

def fuzzy_match_ledger(extracted_name):
    if not os.path.exists(LEDGER_CSV): return {"match": None, "suggested": False}
    df = pd.read_csv(LEDGER_CSV)
    df.columns = [c.strip() for c in df.columns]
    col = df.columns[0]
    names = df[col].dropna().astype(str).tolist()
    clean_target = str(extracted_name).strip()
    
    if clean_target.lower() in ["cash", "cash account", ""]: return {"match": "Cash", "suggested": False}
    for name in names:
        if clean_target.lower() == name.lower(): return {"match": name, "suggested": False}
    matches = difflib.get_close_matches(clean_target, names, n=1, cutoff=0.5)
    return {"match": matches[0], "suggested": True} if matches else {"match": None, "suggested": False}

def fuzzy_match_item(keyword):
    if not os.path.exists(INVENTORY_CSV): return None
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
    xml = "<ENVELOPE>\n  <HEADER>\n    <TALLYREQUEST>Import Data</TALLYREQUEST>\n  </HEADER>\n  <BODY>\n    <DATA>\n      <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
    xml += f"        <VOUCHER VCHTYPE=\"{voucher_type}\" ACTION=\"Create\" OBJVIEW=\"AccountingVoucherView\">\n"
    xml += f"          <DATE>{date_str}</DATE>\n          <VOUCHERTYPENAME>{voucher_type}</VOUCHERTYPENAME>\n          <PARTYLEDGERNAME>{party_name}</PARTYLEDGERNAME>\n"
    xml += f"          <ALLLEDGERENTRIES.LIST>\n            <LEDGERNAME>{party_name}</LEDGERNAME>\n            <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>\n            <AMOUNT>-{total_value:.2f}</AMOUNT>\n          </ALLLEDGERENTRIES.LIST>\n"
    xml += f"          <ALLLEDGERENTRIES.LIST>\n            <LEDGERNAME>Sales Account</LEDGERNAME>\n            <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n            <AMOUNT>{assessable_value:.2f}</AMOUNT>\n          </ALLLEDGERENTRIES.LIST>\n"
    xml += f"          <ALLLEDGERENTRIES.LIST>\n            <LEDGERNAME>CGST @ 9%</LEDGERNAME>\n            <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n            <AMOUNT>{cgst:.2f}</AMOUNT>\n          </ALLLEDGERENTRIES.LIST>\n"
    xml += f"          <ALLLEDGERENTRIES.LIST>\n            <LEDGERNAME>SGST @ 9%</LEDGERNAME>\n            <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n            <AMOUNT>{sgst:.2f}</AMOUNT>\n          </ALLLEDGERENTRIES.LIST>\n"
    for itm in items:
        xml += "          <ALLINVENTORYENTRIES.LIST>\n"
        xml += f"            <STOCKITEMNAME>{itm['Item']}</STOCKITEMNAME>\n            <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>\n"
        xml += f"            <RATE>₹{itm['Rate']}/Nos</RATE>\n            <AMOUNT>{itm['Total']:.2f}</AMOUNT>\n"
        xml += f"            <ACTUALQTY> {itm['Qty']} Nos</ACTUALQTY>\n            <BILLEDQTY> {itm['Qty']} Nos</BILLEDQTY>\n"
        xml += f"            <HSNCODE>{itm['HSN']}</HSNCODE>\n          </ALLINVENTORYENTRIES.LIST>\n"
    xml += "        </VOUCHER>\n      </TALLYMESSAGE>\n    </DATA>\n  </BODY>\n</ENVELOPE>"
    return xml

@celery_engine.task(name="tasks.execute_heavy_inference", bind=True)
def execute_heavy_inference(self, audio_path: str, text_narration: str, source_mode: str = "Voice_Pipeline"):
    task_id = self.request.id
    input_display = text_narration if text_narration else "Audio Telemetry Segment Track"
    save_audit_entry(task_id, source_mode, input_display, "Processing...", "RUNNING")
    
    try:
        if audio_path and os.path.exists(audio_path):
            model = get_stt_model()
            segments, _ = model.transcribe(
                audio_path, beam_size=5, 
                hotwords="hsn gst ledger cash bahi sharmaji khata invoice macbook asus mouse total",
                language=None
            )
            transcript_string = "".join([s.text for s in segments]).strip()
        else:
            transcript_string = text_narration.strip()

        update_audit_entry(task_id, transcript=transcript_string)

        system_prompt = (
            "Extract entities into JSON. Rules: Default voucher_type to 'Sales'. "
            "Format strictly: {\"voucher_type\": \"Sales\", \"party_name\": \"Name\", \"items\": [{\"item_keyword\": \"keyword\", \"quantity\": 1.0, \"rate\": 100.0}]}"
        )
        response = ollama.chat(model='llama3', messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': transcript_string}], format='json')
        extracted_data = json.loads(response['message']['content'].strip())

        raw_party = extracted_data.get("party_name", "").strip()
        ledger_res = fuzzy_match_ledger(raw_party)
        verified_party = ledger_res["match"]
        
        if not verified_party:
            update_audit_entry(task_id, status="LEDGER_MISMATCH")
            return {"status": "LEDGER_NOT_FOUND", "missing_party": raw_party if raw_party else "Unknown New Client", "transcript": transcript_string}

        processed_items = []
        assessable_value = 0.0
        v_type = extracted_data.get("voucher_type", "Sales")

        for itm in extracted_data.get("items", []):
            kw = itm.get("item_keyword", "")
            item_match = fuzzy_match_item(kw)
            if not item_match:
                update_audit_entry(task_id, status="ITEM_MISMATCH")
                return {"status": "ITEM_NOT_FOUND", "missing_keyword": kw if kw else "Unknown Stock Item", "transcript": transcript_string}
            
            qty = float(itm.get("quantity", 1))
            rate = float(itm.get("rate", 0))
            line_total = round(qty * rate, 2)
            assessable_value += line_total
            processed_items.append({"Item": item_match["name"], "Qty": qty, "Rate": rate, "Total": line_total, "HSN": item_match["hsn"]})

        cgst = round(assessable_value * 0.09, 2)
        sgst = round(assessable_value * 0.09, 2)
        total_value = round(assessable_value + cgst + sgst, 2)

        generated_xml = generate_tally_xml(v_type, verified_party, processed_items, assessable_value, cgst, sgst, total_value)
        update_audit_entry(task_id, status="SUCCESS", voucher_type=v_type, party_name=verified_party, items=processed_items, assessable=assessable_value, cgst=cgst, sgst=sgst, total_value=total_value, xml_payload=generated_xml)

        return {
            "status": "SUCCESS", "voucher_type": v_type, "party_name": verified_party,
            "items": processed_items, "assessable_value": assessable_value, "cgst": cgst, "sgst": sgst,
            "total_value": total_value, "transcript": transcript_string, "xml_preview": generated_xml
        }
    except Exception as e:
        update_audit_entry(task_id, status="FAILED")
        return {"status": "FAILED", "error": str(e)}
