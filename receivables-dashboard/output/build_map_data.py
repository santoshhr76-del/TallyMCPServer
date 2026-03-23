import json

parties_raw = [
  {"party_name": "BAJRANG DUDH DAIRY & GEN (RK PURAM)", "gstin": "", "state": "", "pincode": "313002", "addresses": ["Hospital Road"], "phone": "", "email": "", "outstanding": 14827.0},
  {"party_name": "MAYANK TRADERS SEC-4", "gstin": "", "state": "", "pincode": "313705", "addresses": ["HEMANT K PASS GALI ME"], "phone": "", "email": "", "outstanding": 13932.0},
  {"party_name": "KRISHNA DAIRY SEC 3", "gstin": "", "state": "", "pincode": "313001", "addresses": ["PANAKJ UPBHOKTA K PASS"], "phone": "", "email": "", "outstanding": 13136.0},
  {"party_name": "FRIENDS ICE CREAM (UMARDA)", "gstin": "", "state": "", "pincode": "313004", "addresses": ["MANVA KHEDA"], "phone": "", "email": "", "outstanding": 12866.0},
  {"party_name": "MANOJ KIRANA SEC 14", "gstin": "", "state": "", "pincode": "313027", "addresses": ["Main Road Navkar K Samne", "Sec-14"], "phone": "", "email": "", "outstanding": 10459.0},
  {"party_name": "JAI AMBAY JODHPUR SWEETS & BAKERY [2334955]", "gstin": "", "state": "", "pincode": "313024", "addresses": [], "phone": "8619217866", "email": "", "outstanding": 8876.0},
  {"party_name": "Mahalaxmi Misthan Bhandar(Sec.5)", "gstin": "", "state": "", "pincode": "313001", "addresses": ["Manwa Kheda"], "phone": "", "email": "", "outstanding": 8507.0},
  {"party_name": "JAI MEWAR JUICE SEC 6", "gstin": "", "state": "", "pincode": "313031", "addresses": ["KHUSHBOO UPB KE PASS"], "phone": "", "email": "", "outstanding": 6844.0},
  {"party_name": "HEALTH CARE PHARMACY-Sect-3", "gstin": "", "state": "", "pincode": "313011", "addresses": ["Nr Jayesh Mishthan"], "phone": "", "email": "", "outstanding": 6512.0},
  {"party_name": "RAMA MEDICAL STORESEC-14", "gstin": "", "state": "", "pincode": "313003", "addresses": ["SEC.14"], "phone": "", "email": "", "outstanding": 6060.0},
  {"party_name": "ANGAD RESTORENT BALICHA", "gstin": "", "state": "", "pincode": "313001", "addresses": ["Sec 6", "Udaipur"], "phone": "", "email": "", "outstanding": 5587.0},
  {"party_name": "JMB DAIRY (SAVINA)", "gstin": "", "state": "", "pincode": "313803", "addresses": [], "phone": "", "email": "", "outstanding": 4547.0},
  {"party_name": "KRISHNA DUDH DAIRY 14", "gstin": "", "state": "", "pincode": "313001", "addresses": ["92, near Miraz Morning, L-Block", "Sector 14, Shastri Nagar, Udaipur"], "phone": "", "email": "", "outstanding": 4025.0},
  {"party_name": "PATEL DUDH DAIRY & KIRANA (EKLINGPURA)", "gstin": "", "state": "", "pincode": "313024", "addresses": ["LAXMI MISHTAN BHANDAR"], "phone": "", "email": "", "outstanding": 3964.0},
  {"party_name": "NEW BHERUNATH DUTH DAIRY", "gstin": "", "state": "", "pincode": "313005", "addresses": ["KESHAV NAGAR"], "phone": "", "email": "", "outstanding": 3841.0},
  {"party_name": "KAMAKSHI STORE TITARDI", "gstin": "", "state": "", "pincode": "313001", "addresses": ["AMBAMATA GHATI"], "phone": "", "email": "", "outstanding": 3715.0},
  {"party_name": "SALES RETURN - GST [SRGST-12]", "gstin": "", "state": "", "pincode": "", "addresses": [], "phone": "", "email": "", "outstanding": 2749.96},
  {"party_name": "RAJLAXMI BAKERY(SEC.14)", "gstin": "", "state": "", "pincode": "313011", "addresses": ["NEAR BVR SUPERMARKET"], "phone": "", "email": "", "outstanding": 2627.0},
  {"party_name": "MAHAVEER KIRANA STOREA", "gstin": "", "state": "", "pincode": "313903", "addresses": ["SEC,14", "7597991716"], "phone": "", "email": "", "outstanding": 2446.0},
  {"party_name": "JAY BHERAV DERY-Sect-11", "gstin": "", "state": "", "pincode": "313002", "addresses": ["nr alok school", "Udaipur"], "phone": "", "email": "", "outstanding": 1614.0},
  {"party_name": "SK PROVISION STORE SEC 11", "gstin": "", "state": "", "pincode": "313025", "addresses": ["SEC 11", "UDAIPUR"], "phone": "", "email": "", "outstanding": 1309.0},
  {"party_name": "BIKANER PRO. STORE RK PURAM", "gstin": "", "state": "", "pincode": "313003", "addresses": ["SAVINA CIRCLE SEC 9"], "phone": "", "email": "", "outstanding": 836.0},
  {"party_name": "SAI BAKERS SEC 5", "gstin": "", "state": "", "pincode": "313024", "addresses": ["NEAR VISHAL UPBHOKTA"], "phone": "", "email": "", "outstanding": 488.0},
  {"party_name": "OM KIRANA SEC 14", "gstin": "", "state": "", "pincode": "313011", "addresses": ["AAYAD"], "phone": "", "email": "", "outstanding": 307.0},
  {"party_name": "VARSHA RESTAURANT(Titardi)", "gstin": "", "state": "", "pincode": "313001", "addresses": ["Near Shreenath Traders"], "phone": "", "email": "", "outstanding": 227.0}
]

geo = {
    "313001": {"lat": 24.585784, "lng": 73.709223, "place_name": "Udaipur (Girwa)"},
    "313002": {"lat": 24.560026, "lng": 73.708160, "place_name": "Udaipur South (Girwa)"},
    "313003": {"lat": 24.587011, "lng": 73.781289, "place_name": "Udaipur East (Girwa)"},
    "313004": {"lat": 24.595508, "lng": 73.691101, "place_name": "Umarda (Girwa)"},
    "313005": {"lat": None,       "lng": None,        "place_name": "Udaipur 313005"},
    "313011": {"lat": 24.663236, "lng": 73.641145, "place_name": "Badgaon"},
    "313024": {"lat": 24.727643, "lng": 73.659931, "place_name": "Eklingji (Badgaon)"},
    "313025": {"lat": None,       "lng": None,        "place_name": "Udaipur 313025"},
    "313027": {"lat": 24.150662, "lng": 74.041489, "place_name": "Salumbar"},
    "313031": {"lat": 24.448359, "lng": 73.557012, "place_name": "Girwa West"},
    "313705": {"lat": 24.725439, "lng": 73.465190, "place_name": "Gogunda"},
    "313803": {"lat": 24.022356, "lng": 73.558720, "place_name": "Kherwara"},
    "313903": {"lat": 24.023836, "lng": 73.826257, "place_name": "Semari"},
}

pincode_labels = {pc: v["place_name"] for pc, v in geo.items()}

parties = []
for p in parties_raw:
    pc = p["pincode"]
    g = geo.get(pc, {"lat": None, "lng": None, "place_name": ""})
    addr = ", ".join(a for a in p["addresses"] if a)
    parties.append({
        "name": p["party_name"],
        "pincode": pc,
        "state": p.get("state", ""),
        "lat": g["lat"],
        "lng": g["lng"],
        "outstanding": p["outstanding"],
        "max_days_overdue": 0,
        "address": addr,
        "place_name": g["place_name"]
    })

output = {
    "generated_at": "2026-03-21T00:00:00",
    "party_count": len(parties),
    "note": "Geocoded via Nominatim OpenStreetMap",
    "pincode_labels": pincode_labels,
    "parties": parties
}

out_path = "C:/Users/Dell/Documents/TallyMCPServer/receivables-dashboard/output/map_data.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print("Written map_data.json")
print(f"Total parties: {len(parties)}")
plotted = sum(1 for p in parties if p["lat"] is not None)
print(f"With coordinates: {plotted}")
print(f"Missing coordinates: {len(parties)-plotted}")
