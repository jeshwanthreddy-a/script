import os
import gradio as gr
import requests
import time
import pandas as pd
import numpy as np
import scipy.io.wavfile as wavfile
from datetime import datetime

API_URL = "http://127.0.0.1:8000"
LEDGER_CSV = "business_ledgers.csv"
INVENTORY_CSV = "business_inventory.csv"
COMPANY_STATE = "Local State"

def load_csv_data(file_path, default_cols):
    if os.path.exists(file_path):
        try: return pd.read_csv(file_path)
        except: pass
    return pd.DataFrame(columns=default_cols)

def get_party_info(party_name):
    df = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])
    match = df[df["Party Account Name"].str.lower() == party_name.lower()]
    if not match.empty:
        return match.iloc[0]["Region State"]
    return COMPANY_STATE

def get_item_tax_info(item_name):
    df = load_csv_data(INVENTORY_CSV, ["SKU", "Item_Name", "HSN_Code", "Gst_Rate"])
    match = df[df["Item_Name"].str.lower() == item_name.lower()]
    if not match.empty:
        hsn = match.iloc[0]["HSN_Code"]
        raw_rate = str(match.iloc[0]["Gst_Rate"]).replace("%", "").strip()
        try: rate = float(raw_rate) / 100.0
        except: rate = 0.18
        return hsn, rate
    return "HSN-8504", 0.18

def process_voice_pipeline(audio_path, text_overwrite):
    payload = {"source_mode": "Voice_Pipeline"}
    files = None
    
    if audio_path and os.path.exists(audio_path):
        files = {"audio_file": open(audio_path, "rb")}
            
    if not files and text_overwrite:
        payload["text_narration"] = text_overwrite.strip()
    elif not files and not text_overwrite:
        return "⚠️ Please provide a voice recording or text narration.", "", "", gr.update(visible=False), "", "", "", "", "", "", "", ""

    try:
        res = requests.post(f"{API_URL}/v2/statement/parse", data=payload, files=files)
        if files: files["audio_file"].close()
                
        if res.status_code != 200:
            return f"❌ API Error: {res.text}", "", "", gr.update(visible=False), "", "", "", "", "", "", "", ""
        
        task_id = res.json().get("task_id")
        
        for i in range(15):
            time.sleep(1.0 + (i * 0.2))
            status_res = requests.get(f"{API_URL}/v2/statement/status/{task_id}").json()
            
            if status_res.get("status") == "SUCCESS":
                result = status_res.get("result", {})
                
                if result.get("status") == "LEDGER_NOT_FOUND" or "missing_party" in result:
                    missing_party = result.get("missing_party", "Unknown Party")
                    return (f"⚠️ ALERT: Ledger '{missing_party}' missing!", result.get("transcript", ""), "", gr.update(visible=True), missing_party, "Sundry Debtors", "", "", "", "", "", "")
                
                if result.get("status") == "ITEM_NOT_FOUND" or "missing_keyword" in result:
                    missing_item = result.get("missing_keyword", "Unknown Item")
                    return (f"⚠️ ALERT: Item '{missing_item}' missing!", result.get("transcript", ""), "", gr.update(visible=True), missing_item, "Inventory Item", "", "", "", "", "", "")

                party = result.get("party_name", "N/A")
                v_type = result.get("voucher_type", "N/A")
                item_name = result.get("item_name", "General Sale")
                sub_total = float(result.get("total_value", 10.0) if result.get("total_value") else 10.0)
                
                hsn_code, tax_pct = get_item_tax_info(item_name)
                party_state = get_party_info(party)
                is_interstate = (party_state.lower() != COMPANY_STATE.lower()) and (party_state.lower() != "local state")

                if is_interstate:
                    igst_val = sub_total * tax_pct
                    total_tax = igst_val
                    gst_entries_xml = f"""
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>IGST</LEDGERNAME>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{igst_val:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>"""
                    tax_summary_str = f"IGST ({int(tax_pct*100)}%): ₹{igst_val:.2f}"
                else:
                    split_rate = tax_pct / 2.0
                    cgst_val = sub_total * split_rate
                    sgst_val = sub_total * split_rate
                    total_tax = cgst_val + sgst_val
                    gst_entries_xml = f"""
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>CGST</LEDGERNAME>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{cgst_val:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>SGST</LEDGERNAME>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{sgst_val:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>"""
                    tax_summary_str = f"CGST: ₹{cgst_val:.2f} | SGST: ₹{sgst_val:.2f} ({int(tax_pct*100)}%)"

                tax_inclusive_total = sub_total + total_tax
                rounded_total = round(tax_inclusive_total)
                round_off_val = rounded_total - tax_inclusive_total

                parsed_date = result.get("date")
                formatted_date = str(parsed_date).strip() if (parsed_date and len(str(parsed_date).strip()) == 8) else datetime.now().strftime('%Y%m%d')

                raw_xml = f"""<ENVELOPE>
    <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>Test 4</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="{v_type}" ACTION="Create" OBJTYPE="Voucher">
                        <DATE>{formatted_date}</DATE>
                        <EFFECTIVEDATE>{formatted_date}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>{v_type}</VOUCHERTYPENAME>
                        <PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>
                        <ISINVOICE>No</ISINVOICE>
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>{party}</LEDGERNAME>
                            <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <AMOUNT>-{rounded_total:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>Sales</LEDGERNAME>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{sub_total:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>{gst_entries_xml}
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>Round Off</LEDGERNAME>
                            <ISPARTYLEDGER>No</ISPARTYLEDGER>
                            <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                            <AMOUNT>{round_off_val:.2f}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""

                round_off_str = f"₹{round_off_val:+.2f}" if round_off_val != 0 else "₹0.00"

                return (
                    "✅ Verified | Dynamic Tax Applied", 
                    result.get("transcript", text_overwrite), 
                    raw_xml, gr.update(visible=False), "", "", party, v_type, hsn_code, tax_summary_str, round_off_str, f"₹{rounded_total:.2f}"
                )
        
        return "⚠️ Processing timed out.", "", "", gr.update(visible=False), "", "", "", "", "", "", "", ""
    except Exception as e:
        return f"❌ Connection Error: {str(e)}", "", "", gr.update(visible=False), "", "", "", "", "", "", "", ""

def save_and_export_xml(xml_content):
    if not xml_content or xml_content.strip() in ["", "<!-- XML -->"]:
        return "❌ No valid XML payload.", None
    try:
        filename = f"tally_import_voucher_{int(time.time())}.xml"
        with open(filename, "w") as f: f.write(xml_content)
        return f"📦 File created: {filename}", filename
    except Exception as e:
        return f"❌ Export failed: {str(e)}", None

def quick_forge_master(entity_name, group_type):
    try:
        if group_type == "Inventory Item":
            df = load_csv_data(INVENTORY_CSV, ["SKU", "Item_Name", "HSN_Code", "Gst_Rate"])
            df = pd.concat([df, pd.DataFrame([{"SKU": f"SKU-{int(time.time())}", "Item_Name": entity_name, "HSN_Code": "HSN-8504", "Gst_Rate": "18%"}])], ignore_index=True)
            df.to_csv(INVENTORY_CSV, index=False)
        else:
            df = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])
            df = pd.concat([df, pd.DataFrame([{"Party Account Name": entity_name, "Region State": "Local State", "Classification Type": group_type}])], ignore_index=True)
            df.to_csv(LEDGER_CSV, index=False)
            
        requests.post(f"{API_URL}/v2/master/create", json={"name": entity_name, "type": group_type})
        return f"🎉 Added '{entity_name}' successfully!", gr.update(visible=False), df
    except Exception as e:
        return f"❌ Save Failure: {str(e)}", gr.update(visible=True), gr.update()

def handle_csv_import(file_obj, master_type):
    if file_obj is None: return "⚠️ Upload a valid CSV.", pd.DataFrame()
    try:
        uploaded_df = pd.read_csv(file_obj.name)
        target_path = LEDGER_CSV if master_type == "Ledger Masters" else INVENTORY_CSV
        uploaded_df.to_csv(target_path, index=False)
        return f"✅ Master data imported into '{target_path}'!", uploaded_df
    except Exception as e:
        return f"❌ Import Error: {str(e)}", pd.DataFrame()

def handle_csv_export(master_type):
    target_path = LEDGER_CSV if master_type == "Ledger Masters" else INVENTORY_CSV
    if os.path.exists(target_path): return f"📦 Master loaded.", target_path
    return "❌ File missing.", None

init_ledgers = load_csv_data(LEDGER_CSV, ["Party Account Name", "Region State", "Classification Type"])

with gr.Blocks(title="VoiceToTally ERP Suite") as demo:
    gr.Markdown("# 🎙️ VoiceToTally Multilingual Real-Time Dashboard")
    gr.Markdown("### 🔒 Secure On-Premises Voice Processing Pipeline")
    
    # --- TAB 1: LIVE STREAMING ---
    with gr.Tab("Live Streaming Pipeline"):
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("<h2>Option A: Voice Dictation Core</h2>", sanitize_html=False)
                audio_mic = gr.Audio(sources=["microphone"], type="filepath", label="Voice Recording Panel")
                text_override = gr.Textbox(label="Manual Narration Overwrite", placeholder="Enter narration manually...")
                parse_btn = gr.Button("Parse Statement to Draft Pool", variant="primary")
                
                with gr.Group(visible=False) as creation_prompt_pane:
                    gr.Markdown("### 🛠️ Interactive Register Sync")
                    missing_identity = gr.Textbox(label="Missing Account / Inventory Identity")
                    identity_type = gr.Dropdown(choices=["Sundry Debtors", "Sundry Creditors", "Inventory Item"], label="Master Group Placement")
                    forge_btn = gr.Button("Register Master Data (1-Click)", variant="stop")

            with gr.Column(scale=1):
                gr.Markdown("<h2>System Core Status Feed</h2>", sanitize_html=False)
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
                        v_round = gr.Textbox(label="Auto Round Off Amount", interactive=False)
                        v_total = gr.Textbox(label="Grand Total Val", interactive=False)

                gr.Markdown("### ‹/› Verified Tally Structural XML Script Output")
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
        gr.Markdown("## 🗄️ Tally Database Synchronization Hub")
        with gr.Row():
            with gr.Column():
                master_selector = gr.Dropdown(choices=["Ledger Masters", "Inventory Items"], value="Ledger Masters", label="Category")
                csv_file_input = gr.File(label="Upload Tally CSV Sheet", file_types=[".csv"])
                import_btn = gr.Button("Import Data Sheet", variant="primary")
            with gr.Column():
                csv_export_btn = gr.Button("📦 Export Master Sheet")
                csv_export_download = gr.File(label="Download CSV Asset")
        
        sync_status = gr.Textbox(label="Sync Logs", value="System standby.")
        master_dataframe_view = gr.DataFrame(value=init_ledgers, interactive=False, label="Master Registry View")

    # --- TAB 4: STAGING DRAFT AUDIT POOL ---
    with gr.Tab("Staging Draft Audit Pool"):
        gr.Markdown("## 📋 Active Transactions Verification Logs")
        history_table = gr.DataFrame(value=pd.DataFrame(columns=["Timestamp", "Voucher Type", "Party Name", "Amount", "Status"]), interactive=False)

    # --- EVENT BINDINGS ---
    parse_btn.click(
        fn=process_voice_pipeline, inputs=[audio_mic, text_override],
        outputs=[status_box, transcript_box, xml_box, creation_prompt_pane, missing_identity, identity_type, v_party, v_type, v_hsn, v_tax, v_round, v_total]
    )
    forge_btn.click(fn=quick_forge_master, inputs=[missing_identity, identity_type], outputs=[status_box, creation_prompt_pane, master_dataframe_view])
    tally_btn.click(fn=lambda x: "⚠️ Direct network routing disabled. Use XML Export for remote sync.", inputs=[xml_box], outputs=[status_box])
    export_btn.click(fn=save_and_export_xml, inputs=[xml_box], outputs=[status_box, export_file_download])
    import_btn.click(fn=handle_csv_import, inputs=[csv_file_input, master_selector], outputs=[sync_status, master_dataframe_view])
    csv_export_btn.click(fn=handle_csv_export, inputs=[master_selector], outputs=[sync_status, csv_export_download])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
