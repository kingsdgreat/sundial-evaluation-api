import re
import time
import logging
import math
import statistics
import numpy as np
from typing import Tuple, List, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import requests
from dotenv import load_dotenv
import os
from fastapi.middleware.cors import CORSMiddleware


load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Constants
REGRID_API_URL = "https://app.regrid.com/api/v2/parcels/apn"
REGRID_API_TOKEN = os.getenv("REGRID_API_TOKEN")

MILES_TO_DEGREES = 1.0 / 69
INITIAL_SEARCH_RADIUS_MILES = 1.0
MAX_SEARCH_RADIUS_MILES = 5.0
SEARCH_RADIUS_INCREMENT = 1.0
MIN_COMPARABLE_PROPERTIES = 2
MIN_ACREAGE_RATIO = 0.4
MAX_ACREAGE_RATIO = 3.0
PRICE_THRESHOLD = 100000

STATE_ABBREVIATIONS = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
    'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
    'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
    'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO',
    'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ',
    'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH',
    'Oklahoma': 'OK', 'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT',
    'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'
}

class PropertyRequest(BaseModel):
    apn: str
    county: str
    state: str

class ComparableProperty(BaseModel):
    address: str
    price: float
    price_text: str
    acreage: float
    price_per_acre: float
    beds: Optional[str]  
    baths: Optional[str]   
    sqft: Optional[str] 

class ValuationStats(BaseModel):
    min: float
    max: float
    avg: float
    median: float
    std_dev: float

class ValuationResponse(BaseModel):
    target_property: str
    target_acreage: float
    search_radius_miles: float
    comparable_count: int
    estimated_value_avg: Optional[float]
    estimated_value_median: Optional[float]
    price_per_acre_stats: Optional[ValuationStats]
    comparable_properties: List[ComparableProperty]
    outlier_properties: List[ComparableProperty]

app = FastAPI(
    title="Property Valuation API",
    description="API for real estate property valuation based on comparable properties",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_state_abbreviation(state: str) -> str:
    """Convert state name to two-letter abbreviation"""
    state = state.title()  # Normalize input
    if len(state) == 2:
        return state.upper()
    return STATE_ABBREVIATIONS.get(state, state)

def format_regrid_path(state: str, county: str) -> str:
    """Format the path parameter for Regrid API"""
    state_abbr = get_state_abbreviation(state)
    return f"/us/{state_abbr.lower()}/{county.lower().replace(' ', '_')}"

def get_property_info_from_regrid(apn: str, county: str, state: str) -> Optional[Dict]:
    """Fetch property information from Regrid API"""
    try:
        path = format_regrid_path(state, county)
        params = {
            "parcelnumb": apn,
            "path": path,
            "token": REGRID_API_TOKEN,
            "return_zoning": "true",
            "return_matched_buildings": "true",
            "return_matched_addresses": "true",
            "return_enhanced_ownership": "true"
        }
        
        headers = {
            "accept": "application/json"
        }

        response = requests.get(
            REGRID_API_URL,
            params=params,
            headers=headers
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("parcels", {}).get("features"):
            return None

        parcel = data["parcels"]["features"][0]
        coordinates = parcel["geometry"]["coordinates"][0][0]
        properties = parcel["properties"]["fields"]

        return {
            "latitude": coordinates[1],
            "longitude": coordinates[0],
            "acreage": properties.get("deeded_acres") or properties.get("gisacre"),
            "estimated_value": properties.get("parval"),
            "address": properties.get("address"),
            "owner": properties.get("owner"),
            "zoning": properties.get("zoning"),
            "land_value": properties.get("landval"),
            "improvement_value": properties.get("improvval")
        }

    except Exception as e:
        logging.error(f"Error fetching data from Regrid: {str(e)}")
        return None

def calculate_bounding_box(latitude: float, longitude: float, miles: float) -> Tuple[float, float, float, float]:
    offset = miles * MILES_TO_DEGREES
    north = latitude + offset
    south = latitude - offset
    lng_offset = offset * math.cos(latitude * math.pi / 180.0)
    east = longitude + lng_offset
    west = longitude - lng_offset
    return north, south, east, west

def fetch_zillow_data( north: float, south: float, east: float, west: float) -> List[Dict]:
    def get_url_for_page(page_num: int) -> str:
        return f"https://www.zillow.com/homes/recently_sold/{page_num}_p/?searchQueryState=%7B%22pagination%22%3A%7B%22currentPage%22%3A{page_num}%7D%2C%22isMapVisible%22%3Atrue%2C%22mapBounds%22%3A%7B%22west%22%3A{west}%2C%22east%22%3A{east}%2C%22south%22%3A{south}%2C%22north%22%3A{north}%7D%2C%22mapZoom%22%3A14%2C%22usersSearchTerm%22%3A%22%22%2C%22filterState%22%3A%7B%22sort%22%3A%7B%22value%22%3A%22globalrelevanceex%22%7D%2C%22fsba%22%3A%7B%22value%22%3Afalse%7D%2C%22fsbo%22%3A%7B%22value%22%3Afalse%7D%2C%22nc%22%3A%7B%22value%22%3Afalse%7D%2C%22cmsn%22%3A%7B%22value%22%3Afalse%7D%2C%22auc%22%3A%7B%22value%22%3Afalse%7D%2C%22fore%22%3A%7B%22value%22%3Afalse%7D%2C%22rs%22%3A%7B%22value%22%3Atrue%7D%7D%7D"

    url = "https://zillow-com1.p.rapidapi.com/searchByUrl"
    headers = {
        "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
        "x-rapidapi-key": os.getenv("ZILLOW_RAPID_API_KEY")
    }

    all_homes = []
    current_page = 1
    total_pages = None

    while total_pages is None or current_page <= total_pages:
        zillow_url = get_url_for_page(current_page)
        querystring = {"url": zillow_url}

        try:
            response = requests.get(url, headers=headers, params=querystring)
            response.raise_for_status()
            data = response.json()

            if total_pages is None:
                total_pages = data.get('totalPages', 1)

            for property_data in data.get('props', []):
                try:
                    price = property_data.get('price')
                    lot_acres = property_data.get('lotAreaValue')
                    lot_area_unit = property_data.get('lotAreaUnit', '').lower()

                    if not price or not lot_acres or lot_area_unit != 'acres':
                        continue

                    property_dict = {
                        "address": f"{property_data.get('streetAddress', '')}, {property_data.get('city', '')}, {property_data.get('state', '')} {property_data.get('zipcode', '')}",
                        "price": float(price),
                        "price_text": f"${price:,.2f}",
                        "beds": str(property_data.get('bedrooms', 'N/A')),
                        "baths": str(property_data.get('bathrooms', 'N/A')),
                        "sqft": str(property_data.get('livingArea', 'N/A')),
                        "acreage": float(lot_acres),
                        "price_per_acre": float(price) / float(lot_acres) if lot_acres > 0 else None
                    }
                    
                    all_homes.append(property_dict)
                
                except Exception as e:
                    logging.warning(f"Error processing property data: {e}")
                    continue

            current_page += 1
            
        except Exception as e:
            logging.error(f"Error fetching data from Zillow API: {e}")
            break

    return all_homes

def filter_properties_by_acreage(properties: List[Dict], target_acreage: float) -> List[Dict]:
    min_acreage = target_acreage * MIN_ACREAGE_RATIO
    max_acreage = target_acreage * MAX_ACREAGE_RATIO
    
    return [prop for prop in properties 
            if "acreage" in prop 
            and prop["acreage"] is not None 
            and min_acreage <= prop["acreage"] <= max_acreage]

def detect_outliers_iqr(properties: List[Dict], price_key: str = "price_per_acre") -> Tuple[List[Dict], List[Dict]]:
    if not properties:
        return [], []
    
    values = [prop[price_key] for prop in properties if price_key in prop and prop[price_key] is not None]
    
    if len(values) < 4:
        return properties, []
    
    q1 = np.percentile(values, 25)
    q3 = np.percentile(values, 75)
    iqr = q3 - q1
    
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr
    
    valid_properties = []
    outlier_properties = []
    
    for prop in properties:
        if price_key in prop and prop[price_key] is not None:
            if lower_bound <= prop[price_key] <= upper_bound:
                valid_properties.append(prop)
            else:
                outlier_properties.append(prop)
    
    return valid_properties, outlier_properties

def calculate_property_value(target_acreage: float, comparable_properties: List[Dict]) -> Dict:
    if not comparable_properties:
        return {
            "estimated_value_avg": None,
            "estimated_value_median": None,
            "price_per_acre_stats": None,
            "comparable_count": 0
        }
    
    price_per_acre_values = [
        prop["price_per_acre"] for prop in comparable_properties 
        if "price_per_acre" in prop and prop["price_per_acre"] is not None
    ]
    
    avg_price_per_acre = statistics.mean(price_per_acre_values)
    median_price_per_acre = statistics.median(price_per_acre_values)
    
    return {
        "estimated_value_avg": avg_price_per_acre * target_acreage,
        "estimated_value_median": median_price_per_acre * target_acreage,
        "price_per_acre_stats": {
            "min": min(price_per_acre_values),
            "max": max(price_per_acre_values),
            "avg": avg_price_per_acre,
            "median": median_price_per_acre,
            "std_dev": statistics.stdev(price_per_acre_values) if len(price_per_acre_values) > 1 else 0
        },
        "comparable_count": len(comparable_properties)
    }

def find_comparable_properties(
    latitude: float, 
    longitude:    float, 
    target_acreage: float
) -> Tuple[List[Dict], List[Dict], float]:
    search_radius = INITIAL_SEARCH_RADIUS_MILES
    all_properties = []
    final_radius = search_radius
    
    while search_radius <= MAX_SEARCH_RADIUS_MILES:
        logging.info(f"Searching for properties within {search_radius} miles...")
        
        north, south, east, west = calculate_bounding_box(
            latitude, longitude, search_radius
        )
        
        properties = fetch_zillow_data( north, south, east, west)
        filtered_properties = filter_properties_by_acreage(properties, target_acreage)
        
        all_properties.extend(filtered_properties)
        
        unique_properties = []
        addresses = set()
        for prop in all_properties:
            if prop["address"] not in addresses:
                unique_properties.append(prop)
                addresses.add(prop["address"])
        
        all_properties = unique_properties
        
        if len(all_properties) >= MIN_COMPARABLE_PROPERTIES:
            final_radius = search_radius
            break
        
        search_radius += SEARCH_RADIUS_INCREMENT
    
    valid_properties, outlier_properties = detect_outliers_iqr(all_properties)
    
    return valid_properties, outlier_properties, final_radius

@app.post("/valuate-property", response_model=ValuationResponse)
async def valuate_property(property_request: PropertyRequest):
    
    try:
        property_info = get_property_info_from_regrid(
            property_request.apn, 
            property_request.county, 
            property_request.state
        )
        
        if not property_info:
            raise HTTPException(status_code=404, detail="Property information not found")
        
        target_acreage = property_info.get("acreage", 1.0)
        latitude = property_info.get("latitude")
        longitude = property_info.get("longitude")
        
        if not all([latitude, longitude]):
            raise HTTPException(status_code=404, detail="Property coordinates not found")
        
        valid_properties, outlier_properties, final_radius = find_comparable_properties(
             latitude, longitude, target_acreage
        )
        
        valuation_results = calculate_property_value(target_acreage, valid_properties)
        
        return ValuationResponse(
            target_property=f"APN# {property_request.apn}, {property_request.county}, {property_request.state}",
            target_acreage=target_acreage,
            search_radius_miles=final_radius,
            comparable_count=valuation_results['comparable_count'],
            estimated_value_avg=valuation_results['estimated_value_avg'],
            estimated_value_median=valuation_results['estimated_value_median'],
            price_per_acre_stats=ValuationStats(**valuation_results['price_per_acre_stats']) if valuation_results['price_per_acre_stats'] else None,
            comparable_properties=[ComparableProperty(**prop) for prop in valid_properties],
            outlier_properties=[ComparableProperty(**prop) for prop in outlier_properties]
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

