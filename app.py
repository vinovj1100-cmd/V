import streamlit as st
import sqlite3
import pandas as pd
import pytesseract
import pypdf
import re
import io
import os 
import hashlib
import cv2
import logging
import contextlib
from datetime import datetime
from PIL import Image
import numpy as np
from deep_translator import GoogleTranslator
from fpdf import FPDF
import base64
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode

# Configure core logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants & Regular Expressions
os.makedirs("data", exist_ok=True)
DB_PATH = "data/warehouse.db"
SCANNING_ID_REGEX = re.compile(r"\b\d{4,12}-?\d{4}-?\d?\b")

# ------------------ 1. PAGE CONFIG & UI ENHANCEMENTS ------------------
st.set_page_config(page_title="Ozon WMS Pro Enterprise", layout="wide", page_icon="🏢", initial_sidebar_state="expanded")

def get_base64_of_bin_file(bin_file):
    try:
        with open(bin_file, 'rb') as f:
            data = f.read()
        return base64.b64encode(data).decode()
    except FileNotFoundError:
        return "" # Fallback if image is missing

background_base64 = get_base64_of_bin_file('nebula_bg.jpg')

# iOS Glossy & Nebula CSS
st.markdown(f"""
<style>
    .stApp {{
        background-image: url("data:image/jpeg;base64,{background_base64}");
        background-size: cover;
        background-position: center;
        background-attachment: fixed;
    }}
    div[data-testid="stSidebar"], 
    div[data-testid="metric-container"], 
    .stApp > header,
    .stTabs [data-baseweb="tab-list"] {{
        background: rgba(255, 255, 255, 0.1) !important;
        backdrop-filter: blur(15px);
        -webkit-backdrop-filter: blur(15px);
        border-radius: 16px;
        border: 1px solid rgba(255, 255, 255, 0.2);
        box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        color: #ffffff;
    }}
    .stTextInput>div>div>input, .stTextArea>div>div>textarea, .stSelectbox>div>div>div {{
        background: rgba(255, 255, 255, 0.2);
        border-radius: 12px;
        border: 1px solid rgba(255, 255, 255, 0.3);
        color: white;
    }}
    .stButton>button {{
        background: rgba(255, 255, 255, 0.2);
        backdrop-filter: blur(10px);
        border-radius: 12px;
        border: 1px solid rgba(255, 255, 255, 0.4);
        font-weight: 600;
        transition: all 0.3s ease;
    }}
    .stButton>button:hover {{
        background: rgba(255, 255, 255, 0.3);
        transform: translateY(-2px);
    }}
    h1, h2, h3, p, label {{ color: #ffffff !important; }}
</style>
""", unsafe_allow_html=True)

# ------------------ 2. SQLITE DATABASE ENGINE & SECURITY ------------------
@contextlib.contextmanager
def db_session():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        if conn: conn.rollback()
        logger.error(f"Database transaction failure: {e}", exc_info=True)
        st.error(f"⚠️ Storage Layer Error: {e}")
        raise e
    finally:
        if conn: conn.close()

def init_db():
    with db_session() as conn:
        c = conn.cursor()
        # WMS Tables
        c.execute('CREATE TABLE IF NOT EXISTS inventory (SKU TEXT PRIMARY KEY, Product TEXT, Stock INTEGER, Location TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS daily_orders (OrderID TEXT PRIMARY KEY, Status TEXT, RequiredSKUs TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS title_templates (RawTitle TEXT PRIMARY KEY, StandardTitle TEXT)')
        
        # Security & Audit Tables
        c.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, role TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS audit_logs (timestamp TEXT, user TEXT, action TEXT, details TEXT)')
        
        # Seed Default Admin
        c.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?)", ("admin", hashlib.sha256("admin123".encode()).hexdigest(), "Admin"))
        
        # Seed Inventory if empty
        c.execute("SELECT COUNT(*) FROM inventory")
        if c.fetchone()[0] == 0:
            mock_inv = [
                ("APP-IP15-256-BLK", "APPLE IPHONE 15 256GB BLACK", 45, "A1-01"),
                ("APP-IP15P-256-ORG", "APPLE IPHONE 15 PRO COSMIC ORANGE 256GB", 8, "A1-02"),
                ("SAM-S24-512-GRY", "SAMSUNG GALAXY S24 TITAN GRAY 512GB", 12, "B2-15")
            ]
            c.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?)", mock_inv)
            
        # Seed Orders if empty
        c.execute("SELECT COUNT(*) FROM daily_orders")
        if c.fetchone()[0] == 0:
            mock_orders = [
                ("ORD-9981", "Pending", "APP-IP15P-256-ORG, SAM-S24-512-GRY"),
                ("ORD-9982", "Pending", "SAM-S24-512-GRY"),
                ("ORD-9983", "Shipped", "APP-IP15-256-BLK")
            ]
            c.executemany("INSERT INTO daily_orders VALUES (?, ?, ?)", mock_orders)

def log_action(user, action, details=""):
    with db_session() as conn:
        conn.cursor().execute("INSERT INTO audit_logs VALUES (?, ?, ?, ?)", 
                             (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user, action, details))

init_db()

# ------------------ 3. CORE LOGIC & UTILITIES ------------------
@st.cache_data(ttl=60)
def get_inventory():
    with db_session() as conn: return pd.read_sql_query("SELECT * FROM inventory", conn)

@st.cache_data(ttl=60)
def get_orders():
    with db_session() as conn: return pd.read_sql_query("SELECT OrderID as 'Order ID', Status, RequiredSKUs as 'Required SKUs' FROM daily_orders", conn)

def get_templates():
    with db_session() as conn: return pd.read_sql_query("SELECT * FROM title_templates", conn)

def upsert_template(raw, standard):
    with db_session() as conn:
        conn.cursor().execute("INSERT OR REPLACE INTO title_templates (RawTitle, StandardTitle) VALUES (?, ?)", (raw, standard))

def receive_inventory(sku, qty, product="Unknown Product", location="UNASSIGNED"):
    with db_session() as conn:
        c = conn.cursor()
        c.execute("SELECT Stock, Product, Location FROM inventory WHERE SKU = ?", (sku,))
        row = c.fetchone()
        st.cache_data.clear() 
        if row:
            new_stock = row[0] + qty
            final_loc = location if location != "UNASSIGNED" else row[2]
            final_prod = product if product != "Unknown Product" else row[1]
            c.execute("UPDATE inventory SET Stock = ?, Product = ?, Location = ? WHERE SKU = ?", (new_stock, final_prod, final_loc, sku))
            return True 
        else:
            c.execute("INSERT INTO inventory (SKU, Product, Stock, Location) VALUES (?, ?, ?, ?)", (sku, product, qty, location))
            return False 

def deduct_inventory(sku, qty=1):
    with db_session() as conn:
        c = conn.cursor()
        c.execute("SELECT Stock FROM inventory WHERE SKU = ?", (sku,))
        row = c.fetchone()
        if not row:
            st.error(f"❌ Error: SKU {sku} does not exist in inventory.")
            return False
        if row[0] < qty:
            st.error(f"❌ Prevented Error: Insufficient stock for {sku}. Have: {row[0]}, Tried to deduct: {qty}")
            return False
        c.execute("UPDATE inventory SET Stock = Stock - ? WHERE SKU = ?", (qty, sku))
    st.cache_data.clear()
    return True

def update_order_status(order_id, status):
    with db_session() as conn: conn.cursor().execute("UPDATE daily_orders SET Status = ? WHERE OrderID = ?", (status, order_id))
    st.cache_data.clear()

def bulk_update_inventory(df):
    with db_session() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM inventory")
        for _, row in df.iterrows():
            c.execute("INSERT INTO inventory (SKU, Product, Stock, Location) VALUES (?, ?, ?, ?)", 
                      (str(row['SKU']), str(row['Product']), int(row['Stock']), str(row['Location'])))
    st.cache_data.clear()

# --- OCR & Preprocessing ---
def standardize_title(raw_text):
    if not raw_text: return "UNKNOWN"
    text = raw_text.upper().replace("SMARTPHONE ", "").replace("MOBILE PHONE ", "")
    mappings = {"IPHONE": "APPLE IPHONE", " ORANGE": " COSMIC ORANGE", " BLUE": " DEEP BLUE", " GRAY": " TITAN GRAY", " GREY": " TITAN GRAY", " PURPLE": " SANDY PURPLE", "СМАРТФОН": "", "ГБ": "GB"}
    for key, value in mappings.items():
        if key in text and value not in text: text = text.replace(key, value)
    return text.strip()

def extract_text_from_image(image):
    try:
        img_array = np.array(image.convert('RGB'))
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        processed_img = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return pytesseract.image_to_string(processed_img)
    except Exception as e:
        logger.error(f"OCR advanced extraction failed: {e}")
        try: return pytesseract.image_to_string(np.array(image))
        except: return ""

def parse_receiving_data(text_data):
    receiving_items = []
    for line in text_data.strip().split('\n'):
        line = line.strip()
        if not line: continue
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            sku, description = parts[0], parts[1] if len(parts) > 1 else "From Photo/Sheet"
            qty_match = re.search(r'(\d+)\s*$', line)
            location = parts[2] if len(parts) > 2 else "UNASSIGNED"
        else:
            tokens = line.split()
            if not tokens: continue
            sku = tokens[0]
            qty_match = re.search(r'\b(\d+)\b', line)
            description, location = "OCR Text Line Match", "UNASSIGNED"
            
        receiving_items.append({'sku': sku, 'product': description, 'quantity': int(qty_match.group(1)) if qty_match else 1, 'location': location})
    return receiving_items

def generate_user_guide():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Ozon WMS Pro - User Guide", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)
    for title, desc in [("Dashboard:", "View warehouse metrics."), ("Inbound Receiving:", "Scan new SKUs."), ("Inventory Hub:", "Live view of all warehouse stock."), ("PDF Sequencer:", "Map a sequence of tracking IDs to PDF pages.")]:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, desc)
        pdf.ln(4)
        
    # FIX: fpdf2 returns a bytearray natively, so we just convert it to bytes for Streamlit
    return bytes(pdf.output())

# ------------------ 4. AUTHENTICATION LOGIC ------------------
if 'auth' not in st.session_state: st.session_state.auth = None
if 'parsed_items' not in st.session_state: st.session_state.parsed_items = None
if 'photo_text' not in st.session_state: st.session_state.photo_text = ""

def login():
    with st.sidebar:
        st.title("🔐 WMS Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Access System"):
            hashed = hashlib.sha256(p.encode()).hexdigest()
            with db_session() as conn:
                res = conn.cursor().execute("SELECT role FROM users WHERE username=? AND password=?", (u, hashed)).fetchone()
                if res:
                    st.session_state.auth = {"user": u, "role": res[0]}
                    log_action(u, "Login", "Successful system entry")
                    st.rerun()
                else: st.error("Invalid Credentials")

if not st.session_state.auth:
    login()
    st.info("Please login via the sidebar to access the Enterprise WMS.")
    st.stop()

user = st.session_state.auth["user"]
role = st.session_state.auth["role"]
st.session_state.is_admin = (role == "Admin")

# ------------------ 5. SIDEBAR CONFIGURATION ------------------
with st.sidebar:
    st.title(f"☁️ Operator: {user}")
    
    st.divider()
    st.subheader("🔗 Data Synchronization")
    gsheet_link = st.text_input("Drag & Drop Google Sheets Link:", placeholder="https://docs.google.com/spreadsheets/...")
    if st.button("Export Progress to Sheets"):
        if "docs.google.com" in gsheet_link: st.success("✅ Progress staged for sync to Google Sheets API!")
        else: st.warning("Please provide a valid Google Sheets URL.")
            
    st.divider()
    st.subheader("📚 Documentation")
    st.download_button("📥 Download User Guide", generate_user_guide(), "Ozon_WMS_Guide.pdf", "application/pdf", use_container_width=True)
    
    st.divider()
    if st.button("🚪 Logout", type="primary", use_container_width=True):
        log_action(user, "Logout")
        st.session_state.auth = None
        st.rerun()

st.title(f"🏢 Ozon WMS Pro")

# ------------------ 6. TABS LAYOUT ------------------
tabs = st.tabs(["📊 Dashboard", "📥 Inbound Receiving", "📦 Inventory", "🛒 Pick & Pack", "🔙 Returns", "🔍 PDF Sequencer", "⚖️ Auditor", "🔄 Bulk Convert", "🛡️ Admin Panel"])

# --- TAB 1: DASHBOARD ---
with tabs[0]:
    inv_df, orders_df = get_inventory(), get_orders()
    total_stock = inv_df['Stock'].sum() if not inv_df.empty else 0
    low_stock = len(inv_df[inv_df['Stock'] < 10]) if not inv_df.empty else 0
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📦 Total Items in Stock", total_stock)
    m2.metric("⚠️ Low Stock Alerts", low_stock, delta_color="inverse")
    m3.metric("⏳ Pending Orders", len(orders_df[orders_df['Status'] == 'Pending']) if not orders_df.empty else 0)
    m4.metric("✅ Shipped Today", len(orders_df[orders_df['Status'] == 'Shipped']) if not orders_df.empty else 0)

# --- TAB 2: INBOUND RECEIVING ---
with tabs[1]:
    st.markdown("## 📥 **Inbound Receiving Hub**")
    receiving_method = st.radio("Select Receiving Method", ["Manual Scan", "📸 Photo Upload", "📊 Excel Upload"], horizontal=True)
    st.divider()
    
    if receiving_method == "Manual Scan":
        col_in1, col_in2, col_in3 = st.columns(3)
        with col_in1: inbound_sku = st.text_input("Scan / Enter SKU")
        with col_in2: inbound_qty = st.number_input("Quantity", min_value=1, value=1)
        with col_in3: inbound_bin = st.text_input("Bin Location")
        inbound_desc = st.text_input("Product Description (If New SKU)")

        if st.button("➕ Receive Inventory", type="primary"):
            if inbound_sku:
                if receive_inventory(inbound_sku, inbound_qty, inbound_desc or "Unknown Product", inbound_bin or "UNASSIGNED"):
                    st.toast(f"Updated {inbound_sku}: +{inbound_qty} units", icon="📦")
                else: st.toast(f"Created new SKU: {inbound_sku}", icon="✨")
                st.rerun()
            else: st.error("Please enter a SKU.")
                
    elif receiving_method == "📸 Photo Upload":
        photo_upload = st.file_uploader("Upload Sheet Photo", type=["jpg", "png"])
        if photo_upload:
            image = Image.open(photo_upload)
            c1, c2 = st.columns([1, 1])
            with c1: st.image(image, use_container_width=True)
            with c2:
                if st.button("🔍 Extract Text", type="primary"):
                    st.session_state.photo_text = extract_text_from_image(image)
                    st.success("✅ Text extracted!")
            
            if st.session_state.photo_text:
                ext_disp = st.text_area("Extracted Data", value=st.session_state.photo_text, height=200)
                if st.button("✨ Parse Items"): st.session_state.parsed_items = parse_receiving_data(ext_disp)
            
            if st.session_state.parsed_items:
                edited_items = st.data_editor(pd.DataFrame(st.session_state.parsed_items), use_container_width=True)
                if st.button("✅ Receive All"):
                    for _, row in edited_items.iterrows(): receive_inventory(row['sku'], int(row['quantity']), row['product'], row['location'])
                    st.toast("✅ Items Integrated!")
                    st.session_state.parsed_items = None
                    st.rerun()

    elif receiving_method == "📊 Excel Upload":
        excel_up = st.file_uploader("Upload Excel", type=["xlsx", "csv"])
        if excel_up:
            df = pd.read_csv(excel_up) if excel_up.name.endswith('.csv') else pd.read_excel(excel_up)
            available_cols = df.columns.tolist()
            c1, c2, c3, c4 = st.columns(4)
            with c1: sku_col = st.selectbox("SKU", available_cols)
            with c2: prod_col = st.selectbox("Product", available_cols)
            with c3: qty_col = st.selectbox("Quantity", available_cols)
            with c4: loc_col = st.selectbox("Location (Optional)", [None] + available_cols)
            
            preview = [{'sku': str(r[sku_col]).strip(), 'product': str(r[prod_col]).strip(), 'quantity': int(r[qty_col]) if pd.notna(r[qty_col]) else 1, 'location': str(r[loc_col]).strip() if loc_col and pd.notna(r[loc_col]) else "UNASSIGNED"} for _, r in df.iterrows()]
            st.dataframe(pd.DataFrame(preview), use_container_width=True)
            if st.button("✅ Receive All"):
                for item in preview: receive_inventory(item['sku'], item['quantity'], item['product'], item['location'])
                st.success("Committed successfully!")

# --- TAB 3: INVENTORY HUB ---
with tabs[2]:
    st.markdown("### Master Stock List")
    current_inv = get_inventory()
    if not current_inv.empty:
        if st.session_state.is_admin:
            edited_inv = st.data_editor(current_inv, use_container_width=True, num_rows="dynamic")
            if st.button("💾 Save Database Changes", type="primary"):
                bulk_update_inventory(edited_inv)
                log_action(user, "Inventory Update", "Master list modified manually")
                st.toast("✅ Master database updated cleanly!", icon="✅")
                st.rerun()
        else:
            st.dataframe(current_inv, use_container_width=True)
            st.info("🔒 System Admin access required to edit master table.")

# --- TAB 4: PICK & PACK ---
with tabs[3]:
    orders_df = get_orders()
    if not orders_df.empty:
        pending_df = orders_df[orders_df['Status'] == 'Pending']
        if not pending_df.empty:
            col_ord, col_scan = st.columns(2)
            with col_ord:
                sel_order = st.selectbox("Select Order", pending_df['Order ID'].tolist())
                req_skus = [s.strip() for s in pending_df[pending_df['Order ID'] == sel_order].iloc[0]['Required SKUs'].split(',')]
                st.info(f"**Packing:** {sel_order}")
                for sku in req_skus: st.markdown(f"- 📦 `{sku}`")
            with col_scan:
                scanned_input = st.text_area("Barcode Scanner Input")
                if st.button("✅ Verify & Ship", type="primary"):
                    scans = [s.strip() for s in scanned_input.split('\n') if s.strip()]
                    if sorted(scans) == sorted(req_skus):
                        if all(deduct_inventory(sku, 1) for sku in scans):
                            update_order_status(sel_order, 'Shipped')
                            log_action(user, "Order Shipped", sel_order)
                            st.toast("Order shipped!", icon="🚀")
                            st.rerun()
                        else: st.error("Aborted due to inventory errors.")
                    else: st.error("❌ MISMATCH!")

# --- TAB 5: RETURNS ---
with tabs[4]:
    ret_order = st.text_input("Original Order ID")
    ret_sku = st.text_input("Scan Returned SKU")
    ret_reason = st.selectbox("Return Reason", ["Customer Cancelled", "Defective/Damaged"])
    
    if st.button("🔄 Process Return", type="primary") and ret_sku:
        if ret_reason == "Defective/Damaged": st.toast("Logged as Damaged.", icon="⚠️")
        else:
            receive_inventory(ret_sku, 1)
            st.toast("Restocked safely.", icon="✅")
        if ret_order: update_order_status(ret_order, 'Returned')
        log_action(user, "Return Processed", f"{ret_sku} - {ret_reason}")
        st.rerun()

# --- TAB 6: PDF SEQUENCER ---
with tabs[5]:
    st.subheader("🔍 **Pro PDF Label Sequencer**")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        sort_list = st.text_area("🎯 Target Sequence Order", height=300, placeholder="Paste Tracking IDs here...")
        remove_duplicates = st.checkbox("🗑️ Auto-Remove Duplicate IDs", value=True, help="Removes duplicate tracking IDs from your pasted sequence while preserving the order.")
    with col2:
        label_file = st.file_uploader("📄 Upload Labels PDF (Bulk)", type="pdf")
        use_ocr = st.checkbox("Enable OCR Fallback", value=True)

    if st.button("🚀 Scan & Sort PDF", type="primary", use_container_width=True):
        
        # Clean target IDs based on regex to form the expected TABLE sequence
        target_ids_raw = [tid.strip() for tid in sort_list.split('\n') if tid.strip()]
        target_ids = []
        for tid in target_ids_raw:
            match = SCANNING_ID_REGEX.search(tid)
            target_ids.append(match.group() if match else tid)

        # Remove Duplicates Logic from Target List
        if remove_duplicates and target_ids:
            seen = set()
            cleaned_ids = []
            list_duplicates_found = 0
            for tid in target_ids:
                if tid not in seen:
                    seen.add(tid)
                    cleaned_ids.append(tid)
                else:
                    list_duplicates_found += 1
            target_ids = cleaned_ids
            
            if list_duplicates_found > 0:
                st.toast(f"Cleaned {list_duplicates_found} duplicate IDs from sequence!", icon="🧹")

        if not target_ids or not label_file:
            st.warning("⚠️ Provide sequence IDs and upload a PDF.")
        else:
            with st.spinner("Mapping PDF pages via Barcodes & OCR..."):
                try:
                    pdf_reader = pypdf.PdfReader(io.BytesIO(label_file.getvalue()))
                    pdf_writer = pypdf.PdfWriter()
                    
                    # Convert to images using pdf2image
                    images = convert_from_bytes(label_file.getvalue(), dpi=200)
                    id_to_page_map = {}
                    pdf_duplicates_skipped = 0 # Track duplicate pages in the physical PDF
                    
                    for i, img in enumerate(images):
                        page_codes = []
                        barcodes = decode(img)
                        for b in barcodes: 
                            page_codes.extend(SCANNING_ID_REGEX.findall(b.data.decode("utf-8")))
                        
                        if not barcodes and use_ocr: 
                            page_codes.extend(SCANNING_ID_REGEX.findall(pytesseract.image_to_string(img)))
                        
                        for code in set(page_codes): 
                            # ONLY map the page if we haven't seen this ID yet
                            if code not in id_to_page_map:
                                id_to_page_map[code] = {"page": pdf_reader.pages[i], "original_idx": i + 1}
                            else:
                                pdf_duplicates_skipped += 1

                    if pdf_duplicates_skipped > 0:
                        st.toast(f"Skipped {pdf_duplicates_skipped} duplicate page(s) in the uploaded PDF!", icon="📄")

                    results_dataset = []
                    matched_count = 0
                    new_page_counter = 1
                    expected_set = set(target_ids)

                    # Phase 1: Process items in the exact order of the Target Sequence (TABLE)
                    for tid in target_ids:
                        if tid in id_to_page_map:
                            orig_page = id_to_page_map[tid]["original_idx"]
                            conv_page = new_page_counter
                            pdf_writer.add_page(id_to_page_map[tid]["page"])
                            matched_count += 1
                            new_page_counter += 1
                            mis_pdf = ""
                            mis_table = ""
                        else:
                            orig_page = "N/A"
                            conv_page = "N/A"
                            mis_pdf = ""
                            mis_table = tid # ID exists in TABLE but is missing from the uploaded PDF
                            
                        results_dataset.append({
                            "Original pdf page": orig_page,
                            "CONVERTED pdf page": conv_page,
                            "MISMATCH from pdf": mis_pdf,
                            "MISMATCH from TABLE": mis_table
                        })

                    # Phase 2: Identify extra items found in the PDF that were NOT in the target sequence (TABLE)
                    for tid, data in id_to_page_map.items():
                        if tid not in expected_set:
                            results_dataset.append({
                                "Original pdf page": data["original_idx"],
                                "CONVERTED pdf page": "N/A",
                                "MISMATCH from pdf": tid, # ID exists in PDF but was not expected in the TABLE
                                "MISMATCH from TABLE": ""
                            })

                    # Render Output DataFrame
                    if results_dataset:
                        st.dataframe(pd.DataFrame(results_dataset), use_container_width=True)

                    # Provide PDF Generation & Download
                    if matched_count > 0:
                        out_io = io.BytesIO()
                        pdf_writer.write(out_io)
                        log_action(user, "PDF_SEQUENCED", f"Matched {matched_count} pages. Ignored {pdf_duplicates_skipped} PDF duplicates.")
                        st.success(f"✅ Created PDF with {matched_count} sorted pages!")
                        
                        st.download_button(
                            label="📥 Download CONVERTED PDF", 
                            data=out_io.getvalue(), 
                            file_name="sorted_labels.pdf", 
                            mime="application/pdf",
                            use_container_width=True
                        )
                    else:
                        st.error("❌ No matches found in document.")
                        
                except Exception as e:
                    st.error(f"❌ Processing Error: {str(e)}")

# --- TAB 7: AUDITOR ---
with tabs[6]:
    st.subheader("⚖️ Discrepancy & Variance Auditor")
    
    col_a, col_b = st.columns(2)
    with col_a: 
        master_in = st.text_area("**MASTER (Expected)**", height=200, placeholder="Paste system data here...")
    with col_b: 
        scan_in = st.text_area("**SCAN (Actual)**", height=200, placeholder="Paste physical scan data here...")

    if st.button("⚡ Run Discrepancy Analysis", type="primary", use_container_width=True):
        if master_in and scan_in:
            
            # Internal logic function for structural parsing
            def robust_parse_multiline(raw_text):
                data_map = {}
                for line in raw_text.strip().split("\n"):
                    # Regex looks for a 7-digit ID and captures the remaining metadata
                    match = re.search(r"(\b\d{7}\b)(.*)", line)
                    if match:
                        tid = match.group(1)
                        # Splits metadata by tabs, pipes, or multiple spaces
                        elements = {e.strip() for e in re.split(r'[\t|]|\s{2,}', match.group(2).strip()) if e.strip()}
                        data_map[tid] = elements
                return data_map

            m_map = robust_parse_multiline(master_in)
            s_map = robust_parse_multiline(scan_in)
            
            results = []
            # Union of all IDs found in both inputs
            all_ids = sorted(list(set(m_map.keys()) | set(s_map.keys())))
            
            for tid in all_ids:
                exp = m_map.get(tid, set())
                got = s_map.get(tid, set())
                
                # Logic: Exact set comparison
                status = "✅ MATCH" if exp == got else "❌ ERROR"
                
                results.append({
                    "ID": tid, 
                    "Status": status, 
                    "Expected": " | ".join(exp) if exp else "[MISSING]", 
                    "Actual": " | ".join(got) if got else "[MISSING]"
                })
            
            # Create DataFrame
            df_results = pd.DataFrame(results)

            # Apply conditional styling: highlight rows with "❌ ERROR" in light red
            def highlight_errors(row):
                if "❌" in str(row["Status"]):
                    return ['background-color: #ffcccc; color: black'] * len(row)
                return [''] * len(row)

            st.dataframe(
                df_results.style.apply(highlight_errors, axis=1), 
                use_container_width=True,
                height=400
            )
            
            # Log audit results for Admin tracking
            error_count = sum(1 for r in results if "❌" in r["Status"])
            log_action(user, "Audit Performed", f"Total: {len(results)}, Errors: {error_count}")
        else:
            st.warning("⚠️ Please provide data in both Master and Scan fields to analyze.")
            
# --- TAB 8: BULK CONVERT ---
with tabs[7]:
    st.subheader("🔄 Bulk Title Converter")
    white_col = st.text_area("📄 Input (Excel Paste Column Text data)")
    if st.button("✨ Convert & Standardize", type="primary") and white_col:
        lines = [l.strip() for l in white_col.strip().split('\n') if l.strip()]
        try:
            with st.spinner("Processing (Offline-Resilient)..."):
                tdict = dict(zip(get_templates()['RawTitle'], get_templates()['StandardTitle'])) if not get_templates().empty else {}
                uncached = [l.split('\t')[1].strip() if len(l.split('\t')) >= 2 else l.split('\t')[0].strip() for l in lines if (l.split('\t')[1].strip() if len(l.split('\t')) >= 2 else l.split('\t')[0].strip()) not in tdict]
                
                if uncached:
                    try:
                        # Attempt online translation first
                        translator = GoogleTranslator(source='auto', target='en')
                        translated = translator.translate(" ||| ".join(uncached)).split(" ||| ")
                        for raw, val in zip(uncached, translated):
                            std = standardize_title(val)
                            upsert_template(raw, std)
                            tdict[raw] = std
                    except Exception as translation_error:
                        st.warning("⚠️ Offline Mode: Internet translation unavailable. Standardizing local strings only.")
                        logger.warning(f"Translation failed (offline): {translation_error}")
                        # Fallback: Just use standard text cleaning locally without crashing
                        for raw in uncached:
                            std = standardize_title(raw)
                            upsert_template(raw, std)
                            tdict[raw] = std

                outs = [tdict.get(l.split('\t')[1].strip() if len(l.split('\t')) >= 2 else l.split('\t')[0].strip(), "ERR") for l in lines]
                st.text_area("✅ Output", "\n".join(outs), height=200)
                log_action(user, "Bulk Convert", f"Processed {len(lines)} items")
        except Exception as e: 
            st.error(f"Error: {e}")

# --- TAB 9: ADMIN PANEL ---
with tabs[8]:
    if role != "Admin":
        st.warning("Access Restricted to Administrators.")
    else:
        st.subheader("👤 User Management")
        with st.expander("Add New User"):
            new_u = st.text_input("New Username")
            new_p = st.text_input("New Password", type="password")
            new_r = st.selectbox("Role", ["Operator", "Admin"])
            if st.button("Create User"):
                with db_session() as conn:
                    conn.cursor().execute("INSERT INTO users VALUES (?, ?, ?)", (new_u, hashlib.sha256(new_p.encode()).hexdigest(), new_r))
                st.success(f"User {new_u} added.")
                log_action(user, "User Created", new_u)
        
        st.subheader("📜 System Audit Logs")
        with db_session() as conn:
            st.dataframe(pd.read_sql_query("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 100", conn), use_container_width=True)
