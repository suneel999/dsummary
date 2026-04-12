import base64
import json
import os
import random
import re
import secrets
import time
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pdfplumber
import requests
from docxtpl import DocxTemplate
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

load_dotenv()

APP_ROOT = Path(__file__).resolve().parent
WORD_TEMPLATE_DIR = APP_ROOT / "word_template"
DEFAULT_TEMPLATE_PATH = APP_ROOT / "template.docx"
ALLOWED_EXTENSIONS = {"pdf"}
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_MODEL = "gpt-4.1"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "16"))

EMPTY_PATIENT_DATA = {
    "name": "",
    "age/gender": "",
    "ad1": "",
    "ad2": "",
    "mob": "",
    "admision_number": "",
    "umr": "",
    "ward": "",
    "admission_date": "",
    "discharge_date": "",
    "Diagnosis": [],
    "Riskfactors": [],
    "PastHistory": [],
    "ChiefComplaints": "",
    "Course": [],
    "Vitals": {
        "TEMP": "",
        "PR": "",
        "BP": "",
        "SPo2": "",
        "RR": "",
    },
    "Examination": {
        "CVS": "",
        "RS": "",
        "CNS": "",
        "PA": "",
    },
    "Medications": [],
    "cross1": "",
    "cross2": "",
    "cross3": "",
    "cross4": "",
}

DISCHARGE_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "age/gender": {"type": "string"},
        "ad1": {"type": "string"},
        "ad2": {"type": "string"},
        "mob": {"type": "string"},
        "admision_number": {"type": "string"},
        "umr": {"type": "string"},
        "ward": {"type": "string"},
        "admission_date": {"type": "string"},
        "discharge_date": {"type": "string"},
        "Diagnosis": {"type": "array", "items": {"type": "string"}},
        "Riskfactors": {"type": "array", "items": {"type": "string"}},
        "PastHistory": {"type": "array", "items": {"type": "string"}},
        "ChiefComplaints": {"type": "string"},
        "Course": {"type": "array", "items": {"type": "string"}},
        "Vitals": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "TEMP": {"type": "string"},
                "PR": {"type": "string"},
                "BP": {"type": "string"},
                "SPo2": {"type": "string"},
                "RR": {"type": "string"},
            },
            "required": ["TEMP", "PR", "BP", "SPo2", "RR"],
        },
        "Examination": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "CVS": {"type": "string"},
                "RS": {"type": "string"},
                "CNS": {"type": "string"},
                "PA": {"type": "string"},
            },
            "required": ["CVS", "RS", "CNS", "PA"],
        },
        "Medications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "form": {"type": "string"},
                    "name": {"type": "string"},
                    "dosage": {"type": "string"},
                    "freq": {"type": "string"},
                    "time": {"type": "string"},
                },
                "required": ["form", "name", "dosage", "freq", "time"],
            },
        },
        "cross1": {"type": "string"},
        "cross2": {"type": "string"},
        "cross3": {"type": "string"},
        "cross4": {"type": "string"},
    },
    "required": [
        "name",
        "age/gender",
        "ad1",
        "ad2",
        "mob",
        "admision_number",
        "umr",
        "ward",
        "admission_date",
        "discharge_date",
        "Diagnosis",
        "Riskfactors",
        "PastHistory",
        "ChiefComplaints",
        "Course",
        "Vitals",
        "Examination",
        "Medications",
        "cross1",
        "cross2",
        "cross3",
        "cross4",
    ],
}

OPENAI_SYSTEM_PROMPT = """
You are a strict medical discharge summary extractor.
Return only valid JSON that matches the requested schema.
Do not invent facts, defaults, medications, diagnoses, or doctor names.
Preserve wording from the source document as closely as possible.
Use an empty string for missing string fields and an empty array for missing list fields.
Convert dates to YYYY-MM-DD only when a date is present in the source.
""".strip()

OPENAI_USER_PROMPT = """
Extract this discharge summary into the required JSON structure.

Rules:
1. Copy the source text as written whenever possible.
2. Do not summarize, expand, or clean up the medical wording.
3. If a field is absent, use "" for strings and [] for arrays.
4. Separate Riskfactors and PastHistory even if the source combines them.
5. Extract every medication you can find.
6. Keep medicine forms like TAB, CAP, INJ, SYP, and DROP when present.
7. Extract all cross consultation doctor names exactly as written.
8. Output JSON only.
""".strip()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)


def empty_patient_data():
    return deepcopy(EMPTY_PATIENT_DATA)


def normalize_string(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_string_list(value):
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).splitlines()
    normalized = []
    for item in items:
        clean_item = normalize_string(item)
        if clean_item:
            normalized.append(clean_item)
    return normalized


def normalize_nested_fields(source, keys):
    source = source if isinstance(source, dict) else {}
    return {key: normalize_string(source.get(key)) for key in keys}


def normalize_medications(items):
    if not isinstance(items, list):
        return []

    medications = []
    for item in items:
        item = item if isinstance(item, dict) else {}
        medication = {
            "form": normalize_string(item.get("form")).upper(),
            "name": normalize_string(item.get("name")),
            "dosage": normalize_string(item.get("dosage")),
            "freq": normalize_string(item.get("freq")),
            "time": normalize_string(item.get("time")),
        }
        if any(medication.values()):
            medications.append(medication)

    return medications


def normalize_json_data(data):
    data = data if isinstance(data, dict) else {}
    normalized = empty_patient_data()
    normalized.update(
        {
            "name": normalize_string(data.get("name")),
            "age/gender": normalize_string(data.get("age/gender")),
            "ad1": normalize_string(data.get("ad1")),
            "ad2": normalize_string(data.get("ad2")),
            "mob": normalize_string(data.get("mob")),
            "admision_number": normalize_string(data.get("admision_number")),
            "umr": normalize_string(data.get("umr")),
            "ward": normalize_string(data.get("ward")),
            "admission_date": normalize_string(data.get("admission_date")),
            "discharge_date": normalize_string(data.get("discharge_date")),
            "Diagnosis": normalize_string_list(data.get("Diagnosis")),
            "Riskfactors": normalize_string_list(data.get("Riskfactors")),
            "PastHistory": normalize_string_list(data.get("PastHistory")),
            "ChiefComplaints": normalize_string(data.get("ChiefComplaints")),
            "Course": normalize_string_list(data.get("Course")),
            "Vitals": normalize_nested_fields(data.get("Vitals"), ["TEMP", "PR", "BP", "SPo2", "RR"]),
            "Examination": normalize_nested_fields(data.get("Examination"), ["CVS", "RS", "CNS", "PA"]),
            "Medications": normalize_medications(data.get("Medications")),
            "cross1": normalize_string(data.get("cross1")),
            "cross2": normalize_string(data.get("cross2")),
            "cross3": normalize_string(data.get("cross3")),
            "cross4": normalize_string(data.get("cross4")),
        }
    )
    return normalized


def get_available_doctors():
    doctors = {}
    if not WORD_TEMPLATE_DIR.exists():
        return doctors

    for template_file in sorted(WORD_TEMPLATE_DIR.glob("*.docx")):
        filename = template_file.name
        stem_lower = template_file.stem.lower()

        if stem_lower.endswith(" custom"):
            doctor_name = stem_lower[: -len(" custom")].strip()
            template_type = "custom"
        elif stem_lower.endswith(" default"):
            doctor_name = stem_lower[: -len(" default")].strip()
            template_type = "default"
        else:
            continue

        doctor_entry = doctors.setdefault(doctor_name, {"name": doctor_name, "templates": {}})
        doctor_entry["templates"][template_type] = filename

    return dict(sorted(doctors.items()))


def get_template_path(selected_doctor, template_type):
    selected_doctor = normalize_string(selected_doctor).lower()
    template_type = normalize_string(template_type).lower() or "default"
    template_filename = get_available_doctors().get(selected_doctor, {}).get("templates", {}).get(template_type)
    if not template_filename:
        return None

    template_path = WORD_TEMPLATE_DIR / template_filename
    return template_path if template_path.exists() else None


def resolve_posted_template_path(raw_path):
    if not raw_path:
        return DEFAULT_TEMPLATE_PATH if DEFAULT_TEMPLATE_PATH.exists() else None

    candidate = Path(raw_path)
    candidate = candidate.resolve() if candidate.is_absolute() else (APP_ROOT / candidate).resolve()

    if candidate == DEFAULT_TEMPLATE_PATH.resolve() and candidate.exists():
        return candidate

    if WORD_TEMPLATE_DIR.resolve() in candidate.parents and candidate.suffix.lower() == ".docx" and candidate.exists():
        return candidate

    return DEFAULT_TEMPLATE_PATH if DEFAULT_TEMPLATE_PATH.exists() else None


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(pdf_bytes):
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        page_text = [(page.extract_text() or "").strip() for page in pdf.pages]
    return "\n".join(text for text in page_text if text)


def get_openai_settings():
    load_dotenv(override=True)
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable")
    return api_key, model


def encode_pdf_bytes(pdf_bytes):
    encoded = base64.b64encode(pdf_bytes).decode("utf-8")
    return f"data:application/pdf;base64,{encoded}"


def build_openai_messages(filename=None, pdf_bytes=None, pdf_text=None):
    if pdf_bytes is not None:
        safe_name = secure_filename(filename or "upload.pdf") or "upload.pdf"
        user_content = [
            {
                "type": "file",
                "file": {
                    "filename": safe_name,
                    "file_data": encode_pdf_bytes(pdf_bytes),
                },
            },
            {
                "type": "text",
                "text": OPENAI_USER_PROMPT,
            },
        ]
    else:
        user_content = f"{OPENAI_USER_PROMPT}\n\nPDF text:\n\"\"\"\n{pdf_text}\n\"\"\""

    return [
        {"role": "system", "content": OPENAI_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def get_json_schema_response_format():
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "medical_discharge_summary",
            "strict": True,
            "schema": DISCHARGE_SUMMARY_SCHEMA,
        },
    }


def extract_completion_text(response_data):
    message = response_data["choices"][0]["message"]

    if message.get("refusal"):
        raise RuntimeError(message["refusal"])

    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "output_text"} and part.get("text"):
                text_parts.append(part["text"])
        if text_parts:
            return "\n".join(text_parts).strip()

    raise RuntimeError("OpenAI returned an empty response")


def parse_json_response(raw_text):
    text = normalize_string(raw_text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def call_openai(messages, response_format, timeout=90):
    api_key, model = get_openai_settings()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0,
        "messages": messages,
        "response_format": response_format,
    }

    response = requests.post(OPENAI_CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=timeout)
    if response.ok:
        return response.json()

    try:
        error_message = response.json().get("error", {}).get("message") or response.text
    except ValueError:
        error_message = response.text
    raise RuntimeError(f"OpenAI API error ({response.status_code}): {error_message}")


def request_openai_json(messages, max_retries=3, base_delay=2):
    response_formats = [
        get_json_schema_response_format(),
        {"type": "json_object"},
    ]
    last_error = None

    for response_format in response_formats:
        for attempt in range(max_retries):
            try:
                response_data = call_openai(messages, response_format=response_format)
                parsed = parse_json_response(extract_completion_text(response_data))
                return normalize_json_data(parsed)
            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    sleep_time = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    app.logger.warning(
                        "OpenAI extraction attempt %s failed with %s. Retrying in %.2fs.",
                        attempt + 1,
                        response_format["type"],
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                else:
                    app.logger.warning("OpenAI extraction failed with %s: %s", response_format["type"], exc)

    raise last_error or RuntimeError("OpenAI extraction failed")


def get_json_from_pdf_via_openai(pdf_bytes, filename, max_retries=3, base_delay=2):
    try:
        return request_openai_json(
            build_openai_messages(filename=filename, pdf_bytes=pdf_bytes),
            max_retries=max_retries,
            base_delay=base_delay,
        )
    except Exception as pdf_error:
        app.logger.warning("OpenAI PDF extraction failed for %s: %s", filename, pdf_error)
        pdf_text = extract_text_from_pdf(pdf_bytes)
        if not pdf_text:
            raise RuntimeError(
                "OpenAI could not read this PDF. Please try a clearer PDF or use manual entry."
            ) from pdf_error

        try:
            return request_openai_json(
                build_openai_messages(pdf_text=pdf_text),
                max_retries=max_retries,
                base_delay=base_delay,
            )
        except Exception as text_error:
            app.logger.error(
                "OpenAI extraction failed for %s. PDF input error: %s | Text fallback error: %s",
                filename,
                pdf_error,
                text_error,
            )
            raise RuntimeError(
                "OpenAI could not extract structured data from this PDF. Please try another PDF or use manual entry."
            ) from text_error


def format_multiline_field(content):
    if not content:
        return ""
    return "\n".join(content) if isinstance(content, list) else str(content)


def parse_multiline(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def unique_preserve_order(items):
    return list(dict.fromkeys(item for item in items if item))


def format_display_date(value):
    value = normalize_string(value)
    if not value:
        return ""

    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        return value


def get_max_medications(template_type):
    return 20 if template_type == "custom" else 10


def build_document_context(edited_data, max_meds):
    diagnosis = unique_preserve_order(edited_data.get("Diagnosis", []))
    combined_history = unique_preserve_order(
        edited_data.get("Riskfactors", []) + edited_data.get("PastHistory", [])
    )

    context = {
        "umr": edited_data.get("umr", ""),
        "name": edited_data.get("name", "").title(),
        "age": edited_data.get("age/gender", ""),
        "ad1": edited_data.get("ad1", "").title(),
        "ad2": edited_data.get("ad2", "").title(),
        "mob": edited_data.get("mob", ""),
        "admision": edited_data.get("admision_number", ""),
        "ward": edited_data.get("ward", "").upper(),
        "admit": format_display_date(edited_data.get("admission_date", "")),
        "discharge": format_display_date(edited_data.get("discharge_date", "")),
        "Diagnosis": format_multiline_field(diagnosis),
        "ChiefComplaints": normalize_string(edited_data.get("ChiefComplaints", "")).upper(),
        "Riskfactors": format_multiline_field(combined_history),
        "Course": format_multiline_field(edited_data.get("Course", [])),
        "TEMP": edited_data.get("Vitals", {}).get("TEMP", ""),
        "BP": edited_data.get("Vitals", {}).get("BP", ""),
        "PR": edited_data.get("Vitals", {}).get("PR", ""),
        "SPo2": edited_data.get("Vitals", {}).get("SPo2", ""),
        "RR": edited_data.get("Vitals", {}).get("RR", ""),
        "CVS": edited_data.get("Examination", {}).get("CVS", ""),
        "RS": edited_data.get("Examination", {}).get("RS", ""),
        "CNS": edited_data.get("Examination", {}).get("CNS", ""),
        "PA": edited_data.get("Examination", {}).get("PA", ""),
        "current_date": datetime.now().strftime("%d-%b-%Y"),
        "current_time": datetime.now().strftime("%I:%M %p"),
        "cross1": edited_data.get("cross1", ""),
        "cross2": edited_data.get("cross2", ""),
        "cross3": edited_data.get("cross3", ""),
        "cross4": edited_data.get("cross4", ""),
    }

    meds = edited_data.get("Medications") or []
    for index in range(max_meds):
        med = meds[index] if index < len(meds) else {}
        context[f"TAB{index + 1}"] = f"{med.get('form', '')} {med.get('name', '')}".strip()
        context[f"DOSAGE{index + 1}"] = med.get("dosage", "")
        context[f"FREQ{index + 1}"] = med.get("freq", "")
        context[f"TOM{index + 1}"] = med.get("time", "")

    return context


def validate_json_data(data):
    for field in ["name", "age/gender", "admission_date", "discharge_date"]:
        if not normalize_string(data.get(field)):
            raise ValueError(f"Missing required field: {field}")
    return True


@app.errorhandler(413)
def request_entity_too_large(_error):
    flash(f"File is too large. Maximum allowed size is {MAX_UPLOAD_MB}MB.", "danger")
    return redirect(url_for("home"))


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})


@app.route("/", methods=["GET", "POST"])
def home():
    doctors = get_available_doctors()

    if request.method == "POST":
        selected_doctor = normalize_string(request.form.get("selected_doctor")).lower()
        template_type = normalize_string(request.form.get("template_type")).lower() or "default"

        if not selected_doctor:
            flash("Please select a doctor first", "warning")
            return render_template("index.html", doctors=doctors)

        template_path = get_template_path(selected_doctor, template_type)
        if not template_path:
            flash(f"Template not found for {selected_doctor} ({template_type})", "danger")
            return render_template("index.html", doctors=doctors)

        uploaded_file = request.files.get("pdf")
        if not uploaded_file or not uploaded_file.filename:
            flash("Please choose a PDF file first", "danger")
            return redirect(request.url)

        if not allowed_file(uploaded_file.filename):
            flash("Invalid file. Please upload a PDF.", "danger")
            return redirect(request.url)

        try:
            filename = secure_filename(uploaded_file.filename) or "upload.pdf"
            pdf_bytes = uploaded_file.read()
            if not pdf_bytes:
                raise ValueError("The uploaded PDF is empty")

            json_data = get_json_from_pdf_via_openai(pdf_bytes, filename)
            flash("PDF processed successfully! Please review and edit before generating.", "success")
            return render_template(
                "edit_form.html",
                data=json_data,
                is_manual=False,
                template_path=str(template_path),
                selected_doctor=selected_doctor,
                template_type=template_type,
            )
        except Exception as exc:
            app.logger.error("Error processing PDF upload: %s", exc, exc_info=True)
            flash(f"Error processing PDF: {exc}", "danger")
            return redirect(request.url)

    return render_template("index.html", doctors=doctors)


@app.route("/manual", methods=["GET", "POST"])
def manual_entry():
    doctors = get_available_doctors()

    if request.method == "POST":
        selected_doctor = normalize_string(request.form.get("selected_doctor")).lower()
        template_type = normalize_string(request.form.get("template_type")).lower() or "default"

        if not selected_doctor:
            flash("Please select a doctor first", "warning")
            return render_template("index.html", doctors=doctors)

        template_path = get_template_path(selected_doctor, template_type)
        if not template_path:
            flash(f"Template not found for {selected_doctor} ({template_type})", "danger")
            return render_template("index.html", doctors=doctors)

        return render_template(
            "edit_form.html",
            data=empty_patient_data(),
            is_manual=True,
            template_path=str(template_path),
            selected_doctor=selected_doctor,
            template_type=template_type,
        )

    flash("Please select a doctor and template first", "info")
    return redirect(url_for("home"))


@app.route("/review", methods=["GET", "POST"])
def review():
    if request.method == "GET":
        flash("No data to review. Please upload a PDF first.", "warning")
        return redirect(url_for("home"))

    raw_json = request.form.get("json_data", "")
    selected_doctor = normalize_string(request.form.get("selected_doctor")).lower()
    template_type = normalize_string(request.form.get("template_type")).lower() or "default"
    is_manual = normalize_string(request.form.get("is_manual")).lower() == "true"
    template_path = None

    try:
        if not raw_json:
            flash("Data expired. Please start again.", "warning")
            return redirect(url_for("home"))

        session_data = normalize_json_data(json.loads(raw_json))
        max_meds = get_max_medications(template_type)

        edited_data = {
            "umr": normalize_string(request.form.get("umr")) or session_data.get("umr", ""),
            "name": normalize_string(request.form.get("name")) or session_data.get("name", ""),
            "age/gender": normalize_string(request.form.get("age_gender")) or session_data.get("age/gender", ""),
            "ad1": normalize_string(request.form.get("ad1")) or session_data.get("ad1", ""),
            "ad2": normalize_string(request.form.get("ad2")) or session_data.get("ad2", ""),
            "mob": normalize_string(request.form.get("mob")) or session_data.get("mob", ""),
            "admision_number": normalize_string(request.form.get("admision_number"))
            or session_data.get("admision_number", ""),
            "ward": normalize_string(request.form.get("ward")) or session_data.get("ward", ""),
            "admission_date": normalize_string(request.form.get("admission_date"))
            or session_data.get("admission_date", ""),
            "discharge_date": normalize_string(request.form.get("discharge_date"))
            or session_data.get("discharge_date", ""),
            "Diagnosis": parse_multiline(request.form.get("Diagnosis")),
            "Riskfactors": parse_multiline(request.form.get("Riskfactors")),
            "PastHistory": parse_multiline(request.form.get("PastHistory")),
            "ChiefComplaints": normalize_string(request.form.get("ChiefComplaints")),
            "Course": parse_multiline(request.form.get("Course")),
            "Vitals": {
                "TEMP": normalize_string(request.form.get("TEMP")),
                "PR": normalize_string(request.form.get("PR")),
                "BP": normalize_string(request.form.get("BP")),
                "SPo2": normalize_string(request.form.get("SPo2")),
                "RR": normalize_string(request.form.get("RR")),
            },
            "Examination": {
                "CVS": normalize_string(request.form.get("CVS")),
                "RS": normalize_string(request.form.get("RS")),
                "CNS": normalize_string(request.form.get("CNS")),
                "PA": normalize_string(request.form.get("PA")),
            },
            "Medications": [],
            "cross1": normalize_string(request.form.get("cross1")) or session_data.get("cross1", ""),
            "cross2": normalize_string(request.form.get("cross2")) or session_data.get("cross2", ""),
            "cross3": normalize_string(request.form.get("cross3")) or session_data.get("cross3", ""),
            "cross4": normalize_string(request.form.get("cross4")) or session_data.get("cross4", ""),
        }

        for index in range(1, max_meds + 1):
            form_value = normalize_string(request.form.get(f"TAB{index}_form")).upper()
            name_value = normalize_string(request.form.get(f"TAB{index}_name"))
            dosage_value = normalize_string(request.form.get(f"DOSAGE{index}"))
            frequency_value = normalize_string(request.form.get(f"FREQ{index}"))
            time_value = normalize_string(request.form.get(f"TOM{index}"))

            if any([form_value, name_value, dosage_value, frequency_value, time_value]):
                edited_data["Medications"].append(
                    {
                        "form": form_value,
                        "name": name_value,
                        "dosage": dosage_value,
                        "freq": frequency_value,
                        "time": time_value,
                    }
                )

        edited_data = normalize_json_data(edited_data)
        validate_json_data(edited_data)

        template_path = get_template_path(selected_doctor, template_type) or resolve_posted_template_path(
            request.form.get("template_path")
        )
        if not template_path:
            raise FileNotFoundError("Selected Word template was not found")

        context = build_document_context(edited_data, max_meds)
        doc = DocxTemplate(str(template_path))
        doc.render(context)

        output_buffer = BytesIO()
        doc.save(output_buffer)
        output_buffer.seek(0)

        patient_slug = re.sub(r"[^A-Za-z0-9]+", "_", context["name"]).strip("_") or "Patient"
        output_filename = f"Discharge_{patient_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"

        return send_file(
            output_buffer,
            as_attachment=True,
            download_name=output_filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as exc:
        app.logger.error("Error generating document: %s", exc, exc_info=True)
        flash(f"Error during generation: {exc}", "danger")

        try:
            fallback_data = normalize_json_data(edited_data if "edited_data" in locals() else json.loads(raw_json))
        except Exception:
            fallback_data = empty_patient_data()

        return render_template(
            "edit_form.html",
            data=fallback_data,
            is_manual=is_manual,
            template_path=str(template_path or resolve_posted_template_path(request.form.get("template_path")) or ""),
            selected_doctor=selected_doctor,
            template_type=template_type,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
