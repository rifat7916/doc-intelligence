"""Synthetic invoice generator: renders PDFs / scans / images / spreadsheets WITH known ground truth.

Used by the automated tests and by the evaluation dataset builder (eval/generate_dataset.py).
Everything is seeded, so datasets are reproducible.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import cv2
import numpy as np
import pymupdf
from PIL import Image

SUPPLIERS = [
    ("Northwind Traders Ltd", ["12 Harbor Road", "Chattogram 4100"]),
    ("Blue Harbor Supplies", ["88 Market Street", "Dhaka 1212"]),
    ("Greenfield Medical Supplies", ["4 Park Avenue", "Sylhet 3100"]),
    ("Padma Logistics Ltd", ["Plot 17, Road 5", "Dhaka 1230"]),
    ("Meghna Office Systems", ["31 Station Road", "Khulna 9000"]),
    ("Apex Cleaning Co", ["200 Industrial Way", "Gazipur 1700"]),
    ("Lakeside Pharmacy", ["9 Lake Drive", "Rajshahi 6000"]),
    ("Sunrise Equipment LLC", ["77 Sunrise Blvd", "Austin TX 78701"]),
]
CUSTOMERS = [
    ("Care Guide Services", ["House 22, Road 9", "Dhaka 1209"]),
    ("Cottage Home Care", ["45 Elm Street", "Boston MA 02108"]),
    ("Riverside Clinic", ["3 River Lane", "Dhaka 1000"]),
    ("Harmony Senior Living", ["18 Oak Court", "Chicago IL 60601"]),
    ("Unity Health Partners", ["60 Unity Square", "Dhaka 1207"]),
]
ITEMS = [
    ("Wheelchair rental (monthly)", 85.0), ("Nitrile gloves, box of 100", 12.5), ("Home nursing visit", 60.0),
    ("Blood pressure monitor", 45.9), ("Patient transport service", 30.0), ("Medical supplies kit", 120.0),
    ("Cleaning service", 75.0), ("Consulting hours", 95.0), ("Printer paper A4 ream", 6.75),
    ("Disposable aprons, pack of 50", 18.2), ("Hand sanitizer 500ml", 4.4), ("Oxygen concentrator service", 150.0),
]
LABELS = [
    dict(inv="Invoice No:", date="Invoice Date:", cust="Bill To:", sub="Subtotal", total="Total", tax="VAT"),
    dict(inv="Invoice #:", date="Date:", cust="Customer:", sub="Sub Total", total="Grand Total", tax="Tax"),
    dict(inv="Invoice Number:", date="Date of Issue:", cust="Sold To:", sub="Subtotal", total="Total Due", tax="GST"),
    dict(inv="Inv. No.:", date="Issued:", cust="Client:", sub="Net Amount", total="Amount Due", tax="Sales Tax"),
]
DATE_FMTS = ["%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%B %d, %Y"]
SYMBOL = {"USD": "$", "GBP": "\u00a3"}


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Item:
    description: str
    quantity: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return q2(self.quantity * self.unit_price)


@dataclass
class InvoiceSpec:
    number: str | None
    date: dt.date | None
    supplier: str
    supplier_addr: list[str]
    customer: str
    customer_addr: list[str]
    currency: str
    items: list[Item]
    tax_rate: Decimal = Decimal("0.10")
    discount: Decimal = Decimal("0")
    shipping: Decimal = Decimal("0")
    layout: int = 0
    labels: dict = field(default_factory=lambda: LABELS[0])
    date_fmt: str = "%Y-%m-%d"
    eu_format: bool = False
    printed_total: Decimal | None = None  # defect: printed total differs from the real sum
    line_amount_override: dict[int, Decimal] = field(default_factory=dict)  # defect: wrong printed line amount
    defects: list[str] = field(default_factory=list)

    # ---- derived values, exactly as printed ----
    @property
    def line_amounts(self) -> list[Decimal]:
        return [self.line_amount_override.get(i, it.amount) for i, it in enumerate(self.items)]

    @property
    def subtotal(self) -> Decimal:
        return q2(sum((it.amount for it in self.items), Decimal("0")))

    @property
    def tax(self) -> Decimal | None:
        return q2((self.subtotal - self.discount) * self.tax_rate) if self.tax_rate > 0 else None

    @property
    def total(self) -> Decimal:
        if self.printed_total is not None:
            return self.printed_total
        return q2(self.subtotal - self.discount + self.shipping + (self.tax or 0))

    def money(self, x: Decimal | float) -> str:
        s = f"{q2(x):,.2f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".") if self.eu_format else s

    def with_cur(self, x) -> str:
        if self.currency in SYMBOL and not self.eu_format:
            return f"{SYMBOL[self.currency]}{self.money(x)}"
        return f"{self.currency} {self.money(x)}"

    def expected(self) -> dict:
        """Ground truth = what is printed on the document."""
        return {
            "invoice_number": self.number,
            "invoice_date": self.date.isoformat() if self.date else None,
            "supplier_name": self.supplier, "customer_name": self.customer,
            "subtotal": float(self.subtotal), "tax": float(self.tax) if self.tax is not None else None,
            "total": float(self.total), "currency": self.currency,
            "discount": float(self.discount) if self.discount else None,
            "shipping": float(self.shipping) if self.shipping else None,
            "line_items": [{"description": it.description, "quantity": float(it.quantity),
                            "unit_price": float(it.unit_price), "amount": float(a)}
                           for it, a in zip(self.items, self.line_amounts)],
        }

    def reference_text(self) -> str:
        parts = [self.supplier, *self.supplier_addr, self.customer, *self.customer_addr, self.number or "",
                 self.date.strftime(self.date_fmt) if self.date else "", *[i.description for i in self.items]]
        return " ".join(parts)


def random_spec(rng: random.Random, i: int = 0, **over) -> InvoiceSpec:
    sup, sup_addr = rng.choice(SUPPLIERS)
    cus, cus_addr = rng.choice(CUSTOMERS)
    n_items = rng.randint(2, 6)
    items = [Item(d, Decimal(rng.randint(1, 12)), q2(p * rng.choice([0.9, 1.0, 1.1, 1.25])))
             for d, p in rng.sample(ITEMS, n_items)]
    cur = rng.choice(["USD", "USD", "EUR", "BDT", "GBP"])
    number = rng.choice([f"INV-{rng.randint(2025, 2026)}-{rng.randint(1, 9999):04d}",
                         f"BN-{rng.randint(10000, 99999)}", f"{rng.randint(2025, 2026)}/{rng.randint(100, 999)}",
                         f"A-{rng.randint(1000, 9999)}"])
    date = dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 660))
    spec = InvoiceSpec(
        number=number, date=date, supplier=sup, supplier_addr=sup_addr, customer=cus, customer_addr=cus_addr,
        currency=cur, items=items, tax_rate=rng.choice([Decimal("0.10"), Decimal("0.15"), Decimal("0.05")]),
        discount=q2(rng.choice([0, 0, 0, 10, 25])), shipping=q2(rng.choice([0, 0, 15, 20])),
        layout=i % 3, labels=LABELS[i % len(LABELS)], date_fmt=DATE_FMTS[(i // 2) % len(DATE_FMTS)],
        eu_format=(cur == "EUR"))
    for k, v in over.items():
        setattr(spec, k, v)
    return spec


# --------------------------------------------------------------------------------------- PDF rendering
class _Pen:
    def __init__(self, page: pymupdf.Page):
        self.page = page

    def text(self, x, y, s, size=10, bold=False, right=False):
        font = "hebo" if bold else "helv"
        if right:
            x = x - pymupdf.get_text_length(s, fontname=font, fontsize=size)
        self.page.insert_text((x, y), s, fontsize=size, fontname=font)

    def line(self, x0, y0, x1, y1):
        self.page.draw_line((x0, y0), (x1, y1), width=0.7)

    def rect(self, x0, y0, x1, y1, fill):
        self.page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), fill=fill, color=None)


def _render_page(page: pymupdf.Page, s: InvoiceSpec) -> None:
    p, L = _Pen(page), s.labels
    date_s = s.date.strftime(s.date_fmt) if s.date else None
    cur_hdr = f"({s.currency})"
    rows_top = 270
    if s.layout == 0:  # classic
        p.text(50, 70, "INVOICE", 24, True)
        p.text(545, 62, s.supplier, 12, True, right=True)
        for k, a in enumerate(s.supplier_addr):
            p.text(545, 77 + 12 * k, a, 9, right=True)
        y = 120
        if s.number:
            p.text(50, y, f"{L['inv']} {s.number}"); y += 15
        if date_s:
            p.text(50, y, f"{L['date']} {date_s}"); y += 15
        if s.date:
            p.text(50, y, f"Due Date: {(s.date + dt.timedelta(days=30)).strftime(s.date_fmt)}")
        p.text(50, 185, L["cust"], 10, True)
        p.text(50, 200, s.customer)
        for k, a in enumerate(s.customer_addr):
            p.text(50, 213 + 12 * k, a, 9)
        p.line(50, 250, 545, 250)
        for x, h, r in [(50, "Description", False), (330, "Qty", True), (430, f"Unit Price {cur_hdr}", True),
                        (545, f"Amount {cur_hdr}", True)]:
            p.text(x, 264, h, 9, True, right=r)
        p.line(50, 270, 545, 270)
        y = 288
        for it, amt in zip(s.items, s.line_amounts):
            p.text(50, y, it.description, 9.5)
            p.text(330, y, f"{it.quantity:g}", 9.5, right=True)
            p.text(430, y, s.money(it.unit_price), 9.5, right=True)
            p.text(545, y, s.money(amt), 9.5, right=True)
            y += 18
        p.line(330, y - 6, 545, y - 6)
        y += 10
        rows = [(L["sub"], s.with_cur(s.subtotal))]
        if s.discount:
            rows.append(("Discount", "-" + s.with_cur(s.discount)))
        if s.shipping:
            rows.append(("Shipping", s.with_cur(s.shipping)))
        if s.tax is not None:
            rows.append((f"{L['tax']} ({int(s.tax_rate * 100)}%)", s.with_cur(s.tax)))
        for lab, val in rows:
            p.text(400, y, lab, 10); p.text(545, y, val, 10, right=True); y += 16
        p.text(400, y + 4, L["total"], 11, True); p.text(545, y + 4, s.with_cur(s.total), 11, True, right=True)
        p.text(50, 790, "Thank you for your business. Payment terms: Net 30.", 8)
    elif s.layout == 1:  # minimal single column
        p.text(50, 60, s.supplier, 14, True)
        for k, a in enumerate(s.supplier_addr):
            p.text(50, 76 + 12 * k, a, 9)
        y = 120
        if s.number:
            p.text(50, y, f"{L['inv']} {s.number}"); y += 16
        if date_s:
            p.text(50, y, f"{L['date']} {date_s}"); y += 16
        p.text(50, y + 6, f"{L['cust']} {s.customer}"); y += 22
        for a in s.customer_addr:
            p.text(50, y, a, 9); y += 12
        y += 14
        p.text(50, y, "Item", 9, True); p.text(300, y, "Qty", 9, True); p.text(380, y, "Price", 9, True)
        p.text(545, y, "Total", 9, True, right=True); y += 8
        p.line(50, y, 545, y); y += 14
        for it, amt in zip(s.items, s.line_amounts):
            p.text(50, y, it.description, 9.5); p.text(300, y, f"{it.quantity:g}", 9.5)
            p.text(380, y, s.money(it.unit_price), 9.5); p.text(545, y, s.money(amt), 9.5, right=True)
            y += 16
        y += 12
        for lab, val in [(f"{L['sub']}:", s.with_cur(s.subtotal))] + \
                ([("Discount:", "-" + s.with_cur(s.discount))] if s.discount else []) + \
                ([("Shipping:", s.with_cur(s.shipping))] if s.shipping else []) + \
                ([(f"{L['tax']} ({int(s.tax_rate * 100)}%):", s.with_cur(s.tax))] if s.tax is not None else []):
            p.text(50, y, f"{lab} {val}"); y += 15
        p.text(50, y + 5, f"{L['total']}: {s.with_cur(s.total)}", 12, True)
    else:  # modern: header band + grid
        p.rect(0, 0, 595, 110, (0.93, 0.93, 0.93))
        p.text(50, 55, s.supplier, 18, True)
        p.text(50, 75, " , ".join(s.supplier_addr).replace(" , ", ", "), 9)
        p.text(545, 55, "TAX INVOICE", 14, True, right=True)
        y = 150
        if s.number:
            p.text(50, y, L["inv"], 9, True); p.text(160, y, s.number)
        if date_s:
            p.text(330, y, L["date"], 9, True); p.text(430, y, date_s)
        p.text(50, y + 30, L["cust"], 9, True); p.text(160, y + 30, s.customer)
        for k, a in enumerate(s.customer_addr):
            p.text(160, y + 43 + 12 * k, a, 9)
        ty = 260
        p.rect(50, ty - 14, 545, ty + 4, (0.9, 0.93, 0.97))
        for x, h, r in [(55, "Item", False), (330, "Quantity", True), (430, "Unit Price", True), (540, "Total", True)]:
            p.text(x, ty, h, 9, True, right=r)
        y = ty + 24
        for it, amt in zip(s.items, s.line_amounts):
            p.text(55, y, it.description, 9.5); p.text(330, y, f"{it.quantity:g}", 9.5, right=True)
            p.text(430, y, s.money(it.unit_price), 9.5, right=True); p.text(540, y, s.money(amt), 9.5, right=True)
            y += 18
        y += 16
        rows = [(L["sub"], s.with_cur(s.subtotal))]
        if s.discount:
            rows.append(("Discount", "-" + s.with_cur(s.discount)))
        if s.shipping:
            rows.append(("Shipping", s.with_cur(s.shipping)))
        if s.tax is not None:
            rows.append((f"{L['tax']} {int(s.tax_rate * 100)}%", s.with_cur(s.tax)))
        for lab, val in rows:
            p.text(380, y, lab); p.text(540, y, val, right=True); y += 15
        p.text(380, y + 6, L["total"], 11, True); p.text(540, y + 6, s.with_cur(s.total), 11, True, right=True)


def render_pdf_bytes(specs: list[InvoiceSpec]) -> bytes:
    doc = pymupdf.open()
    for s in specs:
        _render_page(doc.new_page(width=595, height=842), s)
    data = doc.tobytes()
    doc.close()
    return data


def write_pdf(specs: list[InvoiceSpec] | InvoiceSpec, path: str) -> str:
    specs = specs if isinstance(specs, list) else [specs]
    with open(path, "wb") as fh:
        fh.write(render_pdf_bytes(specs))
    return path


# ------------------------------------------------------------------------------------ images / scans
def render_gray(spec: InvoiceSpec, dpi: int = 150) -> np.ndarray:
    doc = pymupdf.open(stream=render_pdf_bytes([spec]), filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].copy()
    doc.close()
    return arr


def degrade(gray: np.ndarray, level: str, seed: int = 0) -> np.ndarray:
    """Simulate scanner / phone-camera quality. level: clean | light | medium | heavy."""
    if level == "clean":
        return gray
    rng = np.random.default_rng(seed)
    h, w = gray.shape
    p = {"light": (1.0, 0.4, 6, 0.9), "medium": (2.5, 0.8, 12, 0.7), "heavy": (-3.0, 0.9, 12, 0.7)}[level]
    angle, sigma, noise, scale = p
    img = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    m = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), angle, 1.0)
    img = cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), borderValue=235)
    img = cv2.GaussianBlur(img, (0, 0), sigma)
    img = np.clip(img.astype(np.float32) + rng.normal(0, noise, img.shape), 0, 255)
    img = np.clip(img * 0.92 + 12, 0, 255).astype(np.uint8)  # faded / lower contrast
    return img


def write_image(spec: InvoiceSpec, path: str, level: str = "clean", dpi: int = 150, seed: int = 0) -> str:
    img = degrade(render_gray(spec, dpi), level, seed)
    ext = os.path.splitext(path)[1].lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, 60] if ext in (".jpg", ".jpeg") else []
    cv2.imwrite(path, img, params)
    return path


def write_tiff(specs: list[InvoiceSpec], path: str, level: str = "light") -> str:
    frames = [Image.fromarray(degrade(render_gray(s), level, i)) for i, s in enumerate(specs)]
    frames[0].save(path, save_all=True, append_images=frames[1:], compression="tiff_lzw")
    return path


def write_scanned_pdf(specs: list[InvoiceSpec], path: str, level: str = "medium", dpi: int = 150) -> str:
    doc = pymupdf.open()
    for i, s in enumerate(specs):
        img = degrade(render_gray(s, dpi), level, i)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.tobytes())
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------------------- spreadsheets
HEADER_VARIANTS = [
    ["Invoice No", "Invoice Date", "Supplier", "Customer", "Description", "Qty", "Unit Price", "Line Total",
     "Subtotal", "Discount", "Shipping", "Tax", "Total", "Currency"],
    ["Invoice Number", "Date", "Vendor", "Client", "Item", "Quantity", "Price", "Amount", "Sub Total", "Discount",
     "Freight", "VAT", "Grand Total", "Currency"],
]


def tabular_rows(specs: list[InvoiceSpec], variant: int = 0, repeat_header_fields: bool = True) -> list[list]:
    rows: list[list] = [HEADER_VARIANTS[variant % 2]]
    for s in specs:
        for k, (it, amt) in enumerate(zip(s.items, s.line_amounts)):
            first = k == 0
            show = repeat_header_fields or first
            rows.append([
                s.number if show else None, s.date if show else None, s.supplier if show else None,
                s.customer if show else None, it.description, float(it.quantity), float(it.unit_price), float(amt),
                float(s.subtotal) if show else None, (float(s.discount) or None) if show else None,
                (float(s.shipping) or None) if show else None,
                (float(s.tax) if s.tax is not None else None) if show else None,
                float(s.total) if show else None, s.currency if show else None])
    return rows


def write_xlsx_tabular(specs: list[InvoiceSpec], path: str, variant: int = 0, repeat: bool = True,
                       two_sheets: bool = False) -> str:
    import openpyxl
    wb = openpyxl.Workbook()
    chunks = [specs[: len(specs) // 2], specs[len(specs) // 2:]] if two_sheets and len(specs) > 1 else [specs]
    for i, chunk in enumerate(chunks):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = f"Invoices {i + 1}" if two_sheets else "Invoices"
        for row in tabular_rows(chunk, variant, repeat):
            ws.append(row)
    wb.save(path)
    return path


def write_xls_tabular(specs: list[InvoiceSpec], path: str, variant: int = 0) -> str:
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Invoices")
    style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    for r, row in enumerate(tabular_rows(specs, variant)):
        for c, v in enumerate(row):
            if v is None:
                continue
            if isinstance(v, dt.date):
                ws.write(r, c, v, style)
            else:
                ws.write(r, c, v)
    wb.save(path)
    return path


def write_csv_tabular(specs: list[InvoiceSpec], path: str, delimiter: str = ",", encoding: str = "utf-8",
                      variant: int = 0) -> str:
    with open(path, "w", newline="", encoding=encoding) as fh:
        w = csv.writer(fh, delimiter=delimiter)
        for row in tabular_rows(specs, variant):
            w.writerow(["" if v is None else (v.isoformat() if isinstance(v, dt.date) else v) for v in row])
    return path


def write_xlsx_form(s: InvoiceSpec, path: str) -> str:
    """Form-style invoice (label / value cells), not a table of invoices -> must go through text extraction."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append([s.supplier])
    ws.append(["Invoice No:", s.number])
    ws.append(["Invoice Date:", s.date.isoformat() if s.date else None])
    ws.append(["Bill To:", s.customer])
    ws.append([])
    ws.append(["Description", "Qty", "Unit Price", "Amount"])
    for it, amt in zip(s.items, s.line_amounts):
        ws.append([it.description, float(it.quantity), float(it.unit_price), float(amt)])
    ws.append([])
    ws.append(["Subtotal", None, None, float(s.subtotal)])
    if s.tax is not None:
        ws.append([f"{s.labels['tax']} ({int(s.tax_rate * 100)}%)", None, None, float(s.tax)])
    ws.append(["Total", None, None, float(s.total)])
    ws.append(["Currency:", s.currency])
    wb.save(path)
    return path


# -------------------------------------------------------------------------------------------- corrupt
def write_corrupt(kind: str, path: str, seed: int = 0) -> str:
    rng = random.Random(seed)
    if kind == "truncated_pdf":
        data = render_pdf_bytes([random_spec(rng)])
        open(path, "wb").write(data[: len(data) // 3])
    elif kind == "garbage_pdf":
        open(path, "wb").write(b"%PDF-1.7\n" + bytes(rng.randrange(256) for _ in range(2000)))
    elif kind == "empty":
        open(path, "wb").write(b"")
    elif kind == "truncated_xlsx":
        tmp = path + ".tmp"
        write_xlsx_tabular([random_spec(rng)], tmp)
        data = open(tmp, "rb").read()
        os.remove(tmp)
        open(path, "wb").write(data[: len(data) // 2])
    elif kind == "fake_png":
        open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + bytes(rng.randrange(256) for _ in range(500)))
    elif kind == "text_as_pdf":
        open(path, "wb").write(b"this is just a text file renamed to pdf\n" * 20)
    else:
        raise ValueError(kind)
    return path


# ------------------------------------------------------------------------------- evaluation dataset
def build_dataset(out_dir: str, seed: int = 42) -> list[dict]:
    """Create the labelled evaluation dataset (files + labels.json). Returns the label records."""
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    records: list[dict] = []
    n = [0]

    def spec(**over):
        n[0] += 1
        return random_spec(rng, n[0], **over)

    def add(file, kind, specs, expect_failure=False, defects=None):
        records.append({"file": file, "kind": kind, "expect_failure": expect_failure,
                        "invoices": [s.expected() for s in specs],
                        "defects": defects if defects is not None else sorted({d for s in specs for d in s.defects}),
                        "reference_text": " ".join(s.reference_text() for s in specs)})

    P = lambda name: os.path.join(out_dir, name)  # noqa: E731

    for i in range(10):  # digital PDFs, all layouts / label sets / formats
        s = spec()
        write_pdf(s, P(f"digital_{i + 1:02d}.pdf")); add(f"digital_{i + 1:02d}.pdf", "digital_pdf", [s])
    s = spec(); sp = [s] + [spec() for _ in range(2)]  # 3 invoices in one multi-page PDF
    write_pdf(sp, P("digital_multi_invoice.pdf")); add("digital_multi_invoice.pdf", "digital_pdf", sp)
    for i, level in enumerate(["light", "light", "medium", "medium", "medium", "heavy"]):  # scanned PDFs
        s = spec(); name = f"scanned_{i + 1:02d}_{level}.pdf"
        write_scanned_pdf([s], P(name), level); add(name, "scanned_pdf", [s])
    for i, (ext, level) in enumerate([("png", "clean"), ("png", "light"), ("png", "medium"), ("jpg", "light"),
                                      ("jpg", "medium"), ("jpg", "heavy")]):  # images
        s = spec(); name = f"image_{i + 1:02d}_{level}.{ext}"
        write_image(s, P(name), level, seed=i); add(name, "image", [s])
    sp = [spec(), spec()]
    write_tiff(sp, P("tiff_multipage_light.tiff"), "light"); add("tiff_multipage_light.tiff", "image", sp)
    sp = [spec() for _ in range(4)]  # spreadsheets
    write_xlsx_tabular(sp, P("sheet_tabular_multi.xlsx"), 0); add("sheet_tabular_multi.xlsx", "xlsx", sp)
    sp = [spec() for _ in range(3)]
    write_xlsx_tabular(sp, P("sheet_two_sheets.xlsx"), 1, repeat=False, two_sheets=True)
    add("sheet_two_sheets.xlsx", "xlsx", sp)
    s = spec(); write_xlsx_form(s, P("sheet_form.xlsx")); add("sheet_form.xlsx", "xlsx", [s])
    sp = [spec() for _ in range(2)]
    write_xls_tabular(sp, P("sheet_legacy.xls"), 0); add("sheet_legacy.xls", "xls", sp)
    sp = [spec(currency="USD", eu_format=False) for _ in range(3)]
    write_csv_tabular(sp, P("sheet_comma.csv"), ",", "utf-8"); add("sheet_comma.csv", "csv", sp)
    sp = [spec(currency="USD", eu_format=False) for _ in range(2)]
    write_csv_tabular(sp, P("sheet_semicolon.csv"), ";", "cp1252", 1); add("sheet_semicolon.csv", "csv", sp)
    # injected business defects (the extraction must stay faithful; validation must flag them)
    s = spec(); s.printed_total = q2(s.total + Decimal("37.50")); s.defects = ["TOTAL_MISMATCH"]
    write_pdf(s, P("defect_wrong_total.pdf")); add("defect_wrong_total.pdf", "digital_pdf", [s])
    s = spec(); s.line_amount_override = {0: q2(s.items[0].amount + 11)}; s.defects = ["LINE_CALC_MISMATCH"]
    write_pdf(s, P("defect_wrong_line.pdf")); add("defect_wrong_line.pdf", "digital_pdf", [s])
    s = spec(number=None); s.defects = ["MISSING_FIELD"]
    write_pdf(s, P("defect_no_number.pdf")); add("defect_no_number.pdf", "digital_pdf", [s])
    s = spec(date=None); s.defects = ["MISSING_FIELD"]
    write_pdf(s, P("defect_no_date.pdf")); add("defect_no_date.pdf", "digital_pdf", [s])
    dup = spec(); first = InvoiceSpec(**{**dup.__dict__, "layout": 0}); second = InvoiceSpec(**{**dup.__dict__, "layout": 1})
    write_pdf(first, P("dup_a_original.pdf")); add("dup_a_original.pdf", "digital_pdf", [first], defects=[])
    write_pdf(second, P("dup_b_copy.pdf")); add("dup_b_copy.pdf", "digital_pdf", [second], defects=["DUPLICATE_INVOICE"])
    for i, kind in enumerate(["truncated_pdf", "garbage_pdf", "empty", "truncated_xlsx", "fake_png", "text_as_pdf"]):
        ext = {"truncated_pdf": "pdf", "garbage_pdf": "pdf", "empty": "pdf", "truncated_xlsx": "xlsx",
               "fake_png": "png", "text_as_pdf": "pdf"}[kind]
        name = f"corrupt_{kind}.{ext}"
        write_corrupt(kind, P(name), seed=i)
        records.append({"file": name, "kind": "corrupt", "expect_failure": True, "invoices": [], "defects": [],
                        "reference_text": ""})
    with open(P("labels.json"), "w") as fh:
        json.dump(records, fh, indent=1, ensure_ascii=False)
    return records
