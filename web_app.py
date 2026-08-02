import os
import requests
import gradio as gr

# Backend base URL (local or internal loopback on Render)
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

def parse_text_narration(text_input):
    if not text_input or not text_input.strip():
        return "Please enter a narration statement.", "", "", "", "", "", "", ""
    
    try:
        payload = {"text_narration": text_input.strip()}
        response = requests.post(f"{BACKEND_URL}/v2/statement/parse", json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            status = data.get("status")
            
            if status == "SUCCESS":
                items_summary = ", ".join([f"{item['Item']} (x{item['Qty']})" for item in data.get("items", [])])
                return (
                    f"✅ Processed successfully!",
                    data.get("transcript", text_input),
                    data.get("party_name", "-"),
                    data.get("voucher_type", "-"),
                    items_summary,
                    f"CGST: ₹{data.get('cgst', 0)} | SGST: ₹{data.get('sgst', 0)}",
                    f"₹{data.get('total_value', 0)}",
                    data.get("xml_preview", "")
                )
            elif status == "LEDGER_NOT_FOUND":
                return f"⚠️ Party Ledger not found: {data.get('missing_party')}", text_input, "", "", "", "", "", ""
            elif status == "ITEM_NOT_FOUND":
                return f"⚠️ Item not found in inventory: {data.get('missing_keyword')}", text_input, "", "", "", "", "", ""
            else:
                return f"❌ Error: {data.get('error', 'Unknown error')}", text_input, "", "", "", "", "", ""
        else:
            return f"❌ Backend Error ({response.status_code})", text_input, "", "", "", "", "", ""
            
    except Exception as e:
        return f"❌ Network/Server Error: {str(e)}", text_input, "", "", "", "", "", ""

# Build Gradio Interface
with gr.Blocks(title="VoiceToTally Real-Time Dashboard", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎙️ VoiceToTally Multilingual Real-Time Dashboard")
    gr.Markdown("🔒 **Secure Voice Processing Pipeline**")
    
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### Option A: Voice Dictation / Manual Input")
            narration_input = gr.Textbox(
                label="Manual Narration Overwrite",
                placeholder="e.g. sold 1 mouse to alpha",
                value="sold 1 mouse to alpha"
            )
            submit_btn = gr.Button("Parse Statement to Draft Pool", variant="primary")
            
        with gr.Column(scale=1):
            gr.Markdown("### System Core Status Feed")
            status_output = gr.Textbox(label="Engine Log Output")
            transcript_output = gr.Textbox(label="Processed Narration Transcript")
            
            gr.Markdown("### 📊 Accounting Breakdown & HSN Audit")
            with gr.Row():
                party_output = gr.Textbox(label="Target Party Ledger")
                voucher_output = gr.Textbox(label="Voucher Type")
            with gr.Row():
                items_output = gr.Textbox(label="Automated HSN / Items")
                tax_output = gr.Textbox(label="Auto Tax Splitting")
            total_output = gr.Textbox(label="Grand Total Val")
            
            gr.Markdown("### 🛠️ Verified Tally Structural XML Script Output")
            xml_output = gr.Code(label="Tally XML Payload Stream", language="xml")

    submit_btn.click(
        fn=parse_text_narration,
        inputs=[narration_input],
        outputs=[
            status_output,
            transcript_output,
            party_output,
            voucher_output,
            items_output,
            tax_output,
            total_output,
            xml_output
        ]
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=10000)
