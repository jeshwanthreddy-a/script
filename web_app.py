import os
import re
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import gradio as gr

# Try importing worker backend logic directly
try:
    from worker import parse_statement_logic, transcribe_audio_logic
    DIRECT_WORKER = True
except ImportError:
    DIRECT_WORKER = False

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
LEDGER_CSV = "business_ledgers.csv"
INVENTORY_CSV = "business_inventory.csv"
COMPANY_STATE = "Local State"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1, "single": 1
}

def load_csv_data(file_path, default_cols):
    """Safely load CSV, ensuring required columns always exist."""
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path)
            for col in default_cols:
                if col not in df.columns:
                    df[col] = ""
            return df
        except Exception:
            pass
    
    # Auto-seed initial structure if missing
    df = pd.DataFrame(columns=default_cols)
    if file_path == LEDGER_CSV:
        df = pd.DataFrame([
            {"Party Account Name": "Alpha Corp", "Region State": "Local State", "Classification Type": "Sundry Debtors"},
            {"Party Account Name": "Beta Traders", "Region State": "Maharashtra", "Classification Type": "Sundry Creditors"}
        ])
        df.to_csv(LEDGER_CSV, index=False)
    elif file_path == INVENTORY_CSV:
        df = pd.DataFrame([
            {"SKU": "SKU-001", "Item_Name": "Mouse", "HSN_Code": "HSN-8471", "Gst_Rate": "18%", "Unit_Price": 250.0},
            {"SKU": "SKU-002", "Item_Name": "Keyboard", "HSN_Code": "HSN-8471", "Gst_Rate": "18%", "Unit_Price": 500.0}
        ])
        df.to_csv(INVENTORY_CSV, index=False)
    return df

def get_party_info(party_name):
    df = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])
    if "Party Account Name" in df.columns and not df.empty:
        match = df[df["Party Account Name"].astype(str).str.lower() == str(party_name).lower()]
        if not match.empty:
            return match.iloc[0].get("Region State", COMPANY_STATE)
    return COMPANY_STATE

def get_item_tax_info(item_name):
    df = load_csv_data(INVENTORY_CSV, ["SKU", "Item_Name", "HSN_Code", "Gst_Rate", "Unit_Price"])
    if "Item_Name" in df.columns and not df.empty:
        match = df[df["Item_Name"].astype(str).str.lower() == str(item_name).lower()]
        if not match.empty:
            hsn = match.iloc[0].get("HSN_Code", "HSN-8504")
            raw_rate = str(match.iloc[0].get("Gst_Rate", "18%")).replace("%", "").strip()
            try:
                rate = float(raw_rate) / 100.0
            except Exception:
                rate = 0.18
            
            try:
                unit_price = float(match.iloc[0].get("Unit_Price", 0.0))
            except Exception:
                unit_price = 0.0
                
            return hsn, rate, unit_price
    return "HSN-8504", 0.18, 0.0

def extract_dynamic_entities(transcript):
    """Dynamic regex & string analyzer to extract Party, Item, Qty, and Price from free text."""
    text = transcript.lower()
    
    # 1. Extract Price / Total Value
    # Matches patterns like: "rs.20", "rs 20", "20 rupees", "for 200", "rupees 500", "cost 50"
    price_patterns = [
        r'(?:rs\.?|rupees|inr)\s*(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s*(?:rupees|rs)',
        r'(?:for|amount|cost|worth|at)\s+(?:rs\.?|rupees)?\s*(\d+(?:\.\d+)?)'
    ]
    extracted_price = None
    for pattern in price_patterns:
        match = re.search(pattern, text)
        if match:
            extracted_price = float(match.group(1))
            break

    # 2. Extract Quantity
    qty = 1
    qty_pattern = r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:nos|pcs|pieces|units|items|mouse|keyboards|monitors)?'
    qty_match = re.search(qty_pattern, text)
    if qty_match:
        val = qty_match.group(1)
        if val.isdigit():
            qty = int(val)
        elif val in NUMBER_WORDS:
            qty = NUMBER_WORDS[val]

    # 3. Dynamic Party Detection against LEDGER_CSV
    ledger_df = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])
    detected_party = "Cash"
    for name in ledger_df["Party Account Name"].dropna().unique():
        if str(name).lower() in text:
            detected_party = str(name)
            break
            
    # Fallback party detection if not in CSV yet
    if detected_party == "Cash":
        if "alpha" in text:
            detected_party = "Alpha Corp"
        elif "beta" in text:
            detected_party = "Beta Traders"

    # 4. Dynamic Item Detection against INVENTORY_CSV
    inv_df = load_csv_data(INVENTORY_CSV, ["SKU", "Item_Name", "HSN_Code", "Gst_Rate", "Unit_Price"])
    detected_item = "General Goods"
    for item in inv_df["Item_Name"].dropna().unique():
        if str(item).lower() in text:
            detected_item = str(item)
            break
            
    # Fallback item detection
    if detected_item == "General Goods":
        if "mouse" in text:
            detected_item = "Mouse"
        elif "keyboard" in text:
            detected_item = "Keyboard"
        elif "monitor" in text:
            detected_item = "Monitor"

    # 5. Determine Final Value
    hsn, tax_rate, master_unit_price = get_item_tax_info(detected_item)
    
    if extracted_price is not None:
        final_total_value = extracted_price
    elif master_unit_price > 0:
        final_total_value = qty * master_unit_price
    else:
        final_total_value = 0.0

    voucher_type = "Sales" if any(w in text for w in ["sold", "sale", "sales", "invoice"]) else "Purchase"

    return {
        "status": "SUCCESS",
        "transcript": transcript,
        "party_name": detected_party,
        "voucher_type": voucher_type,
        "items": [{"Item": detected_item, "Qty": qty}],
        "total_value": final_total_value,
        "date": datetime.now().strftime("%Y%m%d")
    }

def transcribe_audio_file(audio_path):
    """Transcribe audio using direct worker logic or Groq Whisper API directly."""
    if DIRECT_WORKER:
        try:
            res = transcribe_audio_logic(audio_path)
            if res:
                return res
        except Exception:
            pass

    if GROQ_API_KEY and audio_path and os.path.exists(audio_path):
        try:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            with open(audio_path, "rb") as f:
                files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
                data = {"model": "whisper-large-v3"}
                res = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data, timeout=30)
                if res.status_code == 200:
                    return res.json().get("text", "")
        except Exception as e:
            print(f"Whisper API error: {e}")

    return ""

def process_voice_pipeline(audio_path, text_overwrite):
    narration = ""

    # Priority 1: Transcribe audio if recorded
    if audio_path and os.path.exists(audio_path):
        transcribed_text = transcribe_audio_file(audio_path)
        if transcribed_text:
            narration = transcribed_text

    # Priority 2: Use manual text input if provided
    if text_overwrite and text_overwrite.strip():
        if not narration:
            narration = text_overwrite.strip()

    if not narration:
        return "⚠️ Please provide a clear voice recording or text narration.", "", "", gr.update(visible=False), "", "", "-", "-", "-", "₹0.00", "₹0.00", "₹0.00"

    try:
        if DIRECT_WORKER:
            result = parse_statement_logic(narration)
        else:
            result = extract_dynamic_entities(narration)

        status = result.get("status", "SUCCESS")
        
        if status == "LEDGER_NOT_FOUND" or "missing_party" in result:
            missing = result.get("missing_party", "Unknown Party")
            return f"⚠️ ALERT: Ledger '{missing}' missing!", result.get("transcript", narration), "", gr.update(visible=True), missing, "Sundry Debtors", "-", "-", "-", "₹0.00", "₹0.00", "₹0.00"

        if status == "ITEM_NOT_FOUND" or "missing_keyword" in result:
            missing = result.get("missing_keyword", "Unknown Item")
            return f"⚠️ ALERT: Item '{missing}' missing!", result.get("transcript", narration), "", gr.update(visible=True), missing, "Inventory Item", "-", "-", "-", "₹0.00", "₹0.00", "₹0.00"

        party = result.get("party_name", "Cash")
        v_type = result.get("voucher_type", "Sales")
        items = result.get("items", [])
        item_name = items[0]["Item"] if items else "General Goods"
        sub_total = float(result.get("total_value", 0.0))

        hsn_code, tax_pct, _ = get_item_tax_info(item_name)
        party_state = get_party_info(party)
        is_interstate = (str(party_state).lower() != COMPANY_STATE.lower()) and (str(party_state).lower() != "local state")

        if is_interstate:
            igst_val = sub_total * tax_pct
            cgst_val, sgst_val = 0.0, 0.0
            total_tax = igst_val
            tax_summary_str = f"IGST ({int(tax_pct*100)}%): ₹{igst_val:.2f}"
            gst_entries_xml = f"""<ALLEDGERENTRIES.LIST>
<LEDGERNAME>IGST</LEDGERNAME>
<ISPARTYLEDGER>No</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{igst_val:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>"""
        else:
            split_rate = tax_pct / 2.0
            cgst_val = sub_total * split_rate
            sgst_val = sub_total * split_rate
            igst_val = 0.0
            total_tax = cgst_val + sgst_val
            tax_summary_str = f"CGST: ₹{cgst_val:.2f} | SGST: ₹{sgst_val:.2f} ({int(tax_pct*100)}%)"
            gst_entries_xml = f"""<ALLEDGERENTRIES.LIST>
<LEDGERNAME>CGST</LEDGERNAME>
<ISPARTYLEDGER>No</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{cgst_val:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>
<ALLEDGERENTRIES.LIST>
<LEDGERNAME>SGST</LEDGERNAME>
<ISPARTYLEDGER>No</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{sgst_val:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>"""

        tax_inclusive_total = sub_total + total_tax
        rounded_total = round(tax_inclusive_total)
        round_off_val = rounded_total - tax_inclusive_total

        parsed_date = result.get("date")
        formatted_date = str(parsed_date).strip() if (parsed_date and len(str(parsed_date).strip()) == 8) else datetime.now().strftime("%Y%m%d")

        raw_xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY>
<IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>Test 4</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="{v_type}" ACTION="Create" OBJTYPE="Voucher">
<DATE>{formatted_date}</DATE>
<EFFECTIVEDATE>{formatted_date}</EFFECTIVEDATE>
<VOUCHERTYPENAME>{v_type}</VOUCHERTYPENAME>
<PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>
<ISINVOICE>No</ISINVOICE>
<ALLEDGERENTRIES.LIST>
<LEDGERNAME>{party}</LEDGERNAME>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-{rounded_total:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>
<ALLEDGERENTRIES.LIST>
<LEDGERNAME>Sales</LEDGERNAME>
<ISPARTYLEDGER>No</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{sub_total:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>
{gst_entries_xml}
<ALLEDGERENTRIES.LIST>
<LEDGERNAME>Round Off</LEDGERNAME>
<ISPARTYLEDGER>No</ISPARTYLEDGER>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{round_off_val:.2f}</AMOUNT>
</ALLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA>
</BODY>
</ENVELOPE>"""

        round_off_str = f"₹{round_off_val:+.2f}" if round_off_val != 0 else "₹0.00"

        return (
            "✅ Audio Dictation Processed & Verified",
            narration,
            raw_xml,
            gr.update(visible=False),
            "",
            "",
            party,
            v_type,
            hsn_code,
            tax_summary_str,
            round_off_str,
            f"₹{rounded_total:.2f}"
        )

    except Exception as e:
        return f"❌ Processing Error: {str(e)}", narration, "", gr.update(visible=False), "", "", "-", "-", "-", "₹0.00", "₹0.00", "₹0.00"

def save_and_export_xml(xml_content):
    if not xml_content or xml_content.strip() in ["", "<!-- XML -->"]:
        return "❌ No valid XML payload.", None
    try:
        filename = f"tally_import_voucher_{int(time.time())}.xml"
        with open(filename, "w") as f:
            f.write(xml_content)
        return f"📦 File created: {filename}", filename
    except Exception as e:
        return f"❌ Export failed: {str(e)}", None

def quick_forge_master(entity_name, group_type):
    try:
        if group_type == "Inventory Item":
            df = load_csv_data(INVENTORY_CSV, ["SKU", "Item_Name", "HSN_Code", "Gst_Rate", "Unit_Price"])
            df = pd.concat([df, pd.DataFrame([{"SKU": f"SKU-{int(time.time())}", "Item_Name": entity_name, "HSN_Code": "HSN-8504", "Gst_Rate": "18%", "Unit_Price": 0.0}])], ignore_index=True)
            df.to_csv(INVENTORY_CSV, index=False)
        else:
            df = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])
            df = pd.concat([df, pd.DataFrame([{"Party Account Name": entity_name, "Region State": "Local State", "Classification Type": group_type}])], ignore_index=True)
            df.to_csv(LEDGER_CSV, index=False)

        return f"🎉 Added '{entity_name}' successfully!", gr.update(visible=False), df
    except Exception as e:
        return f"❌ Save Failure: {str(e)}", gr.update(visible=True), None

def handle_csv_import(file_obj, master_type):
    if file_obj is None:
        return "⚠️ Upload a valid CSV.", pd.DataFrame()
    try:
        uploaded_df = pd.read_csv(file_obj.name)
        target_path = LEDGER_CSV if master_type == "Ledger Masters" else INVENTORY_CSV
        uploaded_df.to_csv(target_path, index=False)
        return f"✅ Master data imported into '{target_path}'!", uploaded_df
    except Exception as e:
        return f"❌ Import Error: {str(e)}", pd.DataFrame()

def handle_csv_export(master_type):
    target_path = LEDGER_CSV if master_type == "Ledger Masters" else INVENTORY_CSV
    if os.path.exists(target_path):
        return f"📦 Master loaded.", target_path
    return "❌ File missing.", None

init_ledgers = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])

# --- UI Construction ---
with gr.Blocks(title="VoiceToTally ERP Suite") as demo:
    gr.Markdown("# 🎙️ VoiceToTally Multilingual Real-Time Dashboard")
    gr.Markdown("### 🔒 Secure On-Premises Voice Processing Pipeline")

    # --- TAB 1: LIVE STREAMING ---
    with gr.Tab("Live Streaming Pipeline"):
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("## Option A: Voice Dictation Core")
                audio_mic = gr.Audio(sources=["microphone", "upload"], type="filepath", label="Voice Recording Panel")
                text_override = gr.Textbox(label="Manual Narration Overwrite", placeholder="Enter narration manually or leave empty when recording...", value="")
                parse_btn = gr.Button("Parse Statement to Draft Pool", variant="primary")

                with gr.Group(visible=False) as creation_prompt_pane:
                    gr.Markdown("### 🛠️ Interactive Register Sync")
                    missing_identity = gr.Textbox(label="Missing Account / Inventory Identity")
                    identity_type = gr.Dropdown(choices=["Sundry Debtors", "Sundry Creditors", "Inventory Item"], label="Master Type")
                    forge_btn = gr.Button("Register Master Data (1-Click)", variant="stop")

            with gr.Column(scale=1):
                gr.Markdown("## System Core Status Feed")
                status_box = gr.Textbox(label="Engine Log Output", interactive=False)
                transcript_box = gr.Textbox(label="Processed Narration Transcript", interactive=False)

                gr.Markdown("### 📊 Accounting Breakdown & HSN Audit")
                with gr.Group():
                    with gr.Row():
                        v_party = gr.Textbox(label="Target Party Ledger", interactive=False)
                        v_type = gr.Textbox(label="Voucher Type", interactive=False)
                    with gr.Row():
                        v_hsn = gr.Textbox(label="Automated HSN / SAC Code", interactive=False)
                        v_tax = gr.Textbox(label="Auto Tax Splitting (CGST/SGST/IGST)", interactive=False)
                    with gr.Row():
                        v_round = gr.Textbox(label="Auto Round Off Amount", interactive=False)
                        v_total = gr.Textbox(label="Grand Total Val", interactive=False)

                gr.Markdown("### 🛠️ Verified Tally Structural XML Script Output")
                xml_box = gr.Textbox(label="Tally XML Payload Stream", max_lines=15, interactive=True)

                with gr.Group():
                    with gr.Row():
                        tally_btn = gr.Button("Push to Local Tally (Port 9000)", variant="primary")
                        export_btn = gr.Button("📦 Generate XML File", variant="success")
                    export_file_download = gr.File(label="Download Transferable Tally XML Asset")

    # --- TAB 2: BATCH DICTATION ---
    with gr.Tab("Batch Dictation Pipeline"):
        gr.Markdown("## 📋 Bulk Continuous Voice Dictation Records Workspace")
        with gr.Row():
            batch_audio = gr.Audio(sources=["microphone"], type="filepath", label="Continuous Bulk Voice Entry")
            batch_status = gr.Textbox(label="Batch Buffer Queue State", value="Queue empty. Ready for recording sequences...")
        batch_submit = gr.Button("Process Bulk Voice Records Sequence", variant="primary")

    # --- TAB 3: MASTER DATA EXCHANGE ---
    with gr.Tab("Tally Master Data Exchange"):
        gr.Markdown("## 💾 Tally Database Synchronization Hub")
        with gr.Row():
            with gr.Column():
                master_selector = gr.Dropdown(choices=["Ledger Masters", "Inventory Items"], value="Ledger Masters", label="Category Selector")
                csv_file_input = gr.File(label="Upload Tally CSV Sheet", file_types=[".csv"])
                import_btn = gr.Button("Import Data Sheet", variant="primary")
            with gr.Column():
                csv_export_btn = gr.Button("📦 Export Master Sheet")
                csv_export_download = gr.File(label="Download CSV Asset")

        sync_status = gr.Textbox(label="Sync Logs", value="System standby.")
        master_dataframe_view = gr.DataFrame(value=init_ledgers, interactive=False, label="Master Registry View")

    # --- TAB 4: STAGING DRAFT AUDIT POOL ---
    with gr.Tab("Staging Draft Audit Pool"):
        gr.Markdown("## 📑 Active Transactions Verification Logs")
        history_table = gr.DataFrame(value=pd.DataFrame(columns=["Timestamp", "Voucher Type", "Party Name", "Amount", "Status"]))

    # --- EVENT BINDINGS ---
    parse_btn.click(
        fn=process_voice_pipeline,
        inputs=[audio_mic, text_override],
        outputs=[
            status_box,
            transcript_box,
            xml_box,
            creation_prompt_pane,
            missing_identity,
            identity_type,
            v_party,
            v_type,
            v_hsn,
            v_tax,
            v_round,
            v_total
        ]
    )

    forge_btn.click(
        fn=quick_forge_master,
        inputs=[missing_identity, identity_type],
        outputs=[status_box, creation_prompt_pane, master_dataframe_view]
    )

    tally_btn.click(
        fn=lambda x: "⚠️ Direct network routing disabled. Use XML Export for remote sync.",
        inputs=[xml_box],
        outputs=[status_box]
    )

    export_btn.click(
        fn=save_and_export_xml,
        inputs=[xml_box],
        outputs=[status_box, export_file_download]
    )

    import_btn.click(
        fn=handle_csv_import,
        inputs=[csv_file_input, master_selector],
        outputs=[sync_status, master_dataframe_view]
    )

    csv_export_btn.click(
        fn=handle_csv_export,
        inputs=[master_selector],
        outputs=[sync_status, csv_export_download]
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
