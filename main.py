import re
import time
import logging
import random
import math
import statistics
import numpy as np
from typing import Tuple, List, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import requests
import uuid
import traceback
import sys
from datetime import datetime


from bs4 import BeautifulSoup
from DrissionPage import ChromiumOptions, WebPage
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from DrissionPage.errors import BrowserConnectError
from fastapi.responses import JSONResponse

# Configure logging
# Configure logging for Vercel environment
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Constants
LOGIN_URL = "https://login.propstream.com/"
ADDRESS_FORMAT = "APN# {0}, {1}, {2}"
EMAIL = "kingsdgreatest@gmail.com"
PASSWORD = "Kanayo147*"
MILES_TO_DEGREES = 1.0 / 69
INITIAL_SEARCH_RADIUS_MILES = 1.0
MAX_SEARCH_RADIUS_MILES = 5.0
SEARCH_RADIUS_INCREMENT = 1.0
MIN_COMPARABLE_PROPERTIES = 2
MIN_ACREAGE_RATIO = 0.4
MAX_ACREAGE_RATIO = 3.0
PRICE_THRESHOLD = 100000

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
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

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

def get_state_abbreviation(state: str) -> str:
    """Convert state name to two-letter abbreviation"""
    # First check if it's already a valid 2-letter code
    if len(state) == 2 and state.upper() in STATE_ABBREVIATIONS.values():
        return state.upper()
    
    # Clean and normalize the state name
    state_name = state.strip().title()
    
    # Get abbreviation from the mapping
    abbr = STATE_ABBREVIATIONS.get(state_name)
    
    if not abbr:
        logging.info(f"State conversion: Input '{state}' -> Using as-is")
        return state.upper()
    
    logging.info(f"State conversion: Input '{state}' -> Converted to '{abbr}'")
    return abbr

def initialize_webpage() -> WebPage:
    co = ChromiumOptions()
    co.headless(True)
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--single-process')
    co.set_argument('--disable-setuid-sandbox')
    co.set_argument('--disable-software-rasterizer')
    port = random.randint(9222, 9322)
    co.set_argument(f'--remote-debugging-port={port}')
    
    # Add cloud browser configuration if available
    if os.environ.get('BROWSERLESS_TOKEN'):
        co.set_argument(f'--remote-debugging-address={os.environ.get("BROWSERLESS_HOST", "chrome.browserless.io")}')
        co.set_argument(f'--remote-debugging-port={os.environ.get("BROWSERLESS_PORT", "3000")}')
    
    page = WebPage(chromium_options=co)
    page.set.window.max()
    return page

def login_to_propstream(page: WebPage, email: str, password: str) -> None:
    try:
        page.get(LOGIN_URL)
        logging.info("Accessing login page")
        
        page.ele("@name=username").input(email)
        logging.info("Email entered")
        
        page.ele("@name=password").input(password)
        logging.info("Password entered")
        
        login_button = page.ele('xpath://*[@id="form-content"]/form/button')
        if login_button:
            login_button.click()
            logging.info("Login button clicked")
        else:
            logging.error("Login button not found")
            raise Exception("Login button not found")
            
        time.sleep(5)
        
        proceed_button = page.ele("@text():Proceed")
        if proceed_button:
            proceed_button.click()
            logging.info("Proceed button clicked")
        else:
            logging.info("No proceed button found - user may already be logged in")
            
    except Exception as e:
        logging.error(f"Login failed with error: {str(e)}")
        raise

def search_property(page: WebPage, address_format: str, apn: str, county: str, state: str) -> None:
    state_abbr = get_state_abbreviation(state)
    address = address_format.format(apn, county, state_abbr)  # Use state_abbr here
    
    try:
        logging.info(f"Searching for address: {address}")
        
        time.sleep(7)
        search_input = page.ele("@type=text")
        if not search_input:
            raise Exception("Search input field not found")
            
        search_input.input(address)
        logging.info("Address entered in search")
        
        time.sleep(3)
        result = page.ele(f"@text()={address}")
        if not result:
            raise Exception("Search result not found")
        
        time.sleep(3)
        result = page.ele(f"@text()={address}")
        if not result:
            raise Exception("Search result not found")
            
        result.click()
        logging.info("Search result clicked")
        
        time.sleep(5)
        property_link = page.ele('xpath://*[@id="root"]/div/div[2]/div/div/div[3]/div[1]/div/section/div[2]/div/div/div/div/div[1]/h3/a')
        if not property_link:
            raise Exception("Property details link not found")
            
        property_link.click()
        logging.info("Property details opened")
        
        time.sleep(5)
        location_pin = page.ele('xpath://*[@id="propertyDetail"]/div/div/div[2]/div/div/div/div[1]/div[1]/div/div/div/div/div[1]/div[1]/div')
        if not location_pin:
            raise Exception("Location pin not found")
            
        location_pin.click()
        logging.info("Location pin clicked")
        
    except Exception as e:
        logging.error(f"Property search failed: {str(e)}")
        raise

def extract_coordinates(html: str) -> Optional[Tuple[float, float]]:
    soup = BeautifulSoup(html, "html.parser")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "maps.google.com/maps" in href and "ll=" in href:
            match = re.search(r"ll=([-+]?\d*\.\d+),([-+]?\d*\.\d+)", href)
            if match:
                try:
                    return float(match.group(1)), float(match.group(2))
                except ValueError:
                    logging.warning("Failed to convert coordinates to float.")
    logging.warning("Coordinates not found in HTML.")
    return None

def extract_property_info(page: WebPage) -> Optional[Dict]:
    try:
        html = page.html
        soup = BeautifulSoup(html, "html.parser")
        property_info = {}
        
        lot_size_elem = soup.find(lambda tag: tag.name == "div" and "Lot Size" in tag.text)
        if lot_size_elem:
            lot_size_text = lot_size_elem.find_next("div").text.strip()
            if "acres" in lot_size_text.lower():
                acreage_match = re.search(r"([\d.]+)\s*acres", lot_size_text.lower())
                if acreage_match:
                    property_info["acreage"] = float(acreage_match.group(1))
            elif "sqft" in lot_size_text.lower() or "sq ft" in lot_size_text.lower():
                sqft_match = re.search(r"([\d,]+)\s*sq", lot_size_text.lower())
                if sqft_match:
                    sqft = float(sqft_match.group(1).replace(",", ""))
                    property_info["acreage"] = sqft / 43560

        value_elem = soup.find(lambda tag: tag.name == "div" and "Estimated Value" in tag.text)
        if value_elem:
            value_text = value_elem.find_next("div").text.strip()
            value_match = re.search(r"\$?([\d,]+)", value_text)
            if value_match:
                property_info["estimated_value"] = float(value_match.group(1).replace(",", ""))
        
        return property_info
    except Exception as e:
        logging.error(f"Failed to extract property info: {e}")
        return None

def calculate_bounding_box(latitude: float, longitude: float, miles: float) -> Tuple[float, float, float, float]:
    offset = miles * MILES_TO_DEGREES
    north = latitude + offset
    south = latitude - offset
    lng_offset = offset * math.cos(latitude * math.pi / 180.0)
    east = longitude + lng_offset
    west = longitude - lng_offset
    return north, south, east, west

def fetch_zillow_data(page: WebPage, north: float, south: float, east: float, west: float) -> List[Dict]:
    def get_url_for_page(page_num: int) -> str:
        return f"https://www.zillow.com/homes/recently_sold/{page_num}_p/?searchQueryState=%7B%22pagination%22%3A%7B%22currentPage%22%3A{page_num}%7D%2C%22isMapVisible%22%3Atrue%2C%22mapBounds%22%3A%7B%22west%22%3A{west}%2C%22east%22%3A{east}%2C%22south%22%3A{south}%2C%22north%22%3A{north}%7D%2C%22mapZoom%22%3A14%2C%22usersSearchTerm%22%3A%22%22%2C%22filterState%22%3A%7B%22sort%22%3A%7B%22value%22%3A%22globalrelevanceex%22%7D%2C%22fsba%22%3A%7B%22value%22%3Afalse%7D%2C%22fsbo%22%3A%7B%22value%22%3Afalse%7D%2C%22nc%22%3A%7B%22value%22%3Afalse%7D%2C%22cmsn%22%3A%7B%22value%22%3Afalse%7D%2C%22auc%22%3A%7B%22value%22%3Afalse%7D%2C%22fore%22%3A%7B%22value%22%3Afalse%7D%2C%22rs%22%3A%7B%22value%22%3Atrue%7D%2C%22sf%22%3A%7B%22value%22%3Afalse%7D%2C%22tow%22%3A%7B%22value%22%3Afalse%7D%2C%22mf%22%3A%7B%22value%22%3Afalse%7D%2C%22con%22%3A%7B%22value%22%3Afalse%7D%2C%22apa%22%3A%7B%22value%22%3Afalse%7D%2C%22manu%22%3A%7B%22value%22%3Afalse%7D%2C%22apco%22%3A%7B%22value%22%3Afalse%7D%7D%2C%22isListVisible%22%3Atrue%7D"

    url = "https://zillow-com1.p.rapidapi.com/searchByUrl"
    headers = {
        "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
        "x-rapidapi-key": "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390"
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
                logging.info(f"Total pages to fetch: {total_pages}")

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
                        "lot_size": f"{lot_acres:.2f} acres",
                        "acreage": float(lot_acres),
                        "price_per_acre": float(price) / float(lot_acres) if lot_acres > 0 else None,
                        "date_sold": property_data.get('dateSold'),
                        "home_type": property_data.get('homeType'),
                        "latitude": property_data.get('latitude'),
                        "longitude": property_data.get('longitude')
                    }
                    
                    all_homes.append(property_dict)
                
                except Exception as e:
                    logging.warning(f"Error processing property data: {e}")
                    continue

            logging.info(f"Completed fetching page {current_page} of {total_pages}")
            current_page += 1
            
        except Exception as e:
            logging.error(f"Error fetching data from Zillow API on page {current_page}: {e}")
            break

    logging.info(f"Successfully processed {len(all_homes)} total properties")
    return all_homes

def filter_properties_by_acreage(properties: List[Dict], target_acreage: float) -> List[Dict]:
    min_acreage = target_acreage * MIN_ACREAGE_RATIO
    max_acreage = target_acreage * MAX_ACREAGE_RATIO
    
    filtered_properties = []
    for prop in properties:
        if "acreage" in prop and prop["acreage"] is not None:
            if min_acreage <= prop["acreage"] <= max_acreage:
                filtered_properties.append(prop)
    
    return filtered_properties

def detect_outliers_iqr(properties: List[Dict], price_key: str = "price_per_acre") -> Tuple[List[Dict], List[Dict]]:
    if not properties:
        return [], []
    
    values = [prop[price_key] for prop in properties if price_key in prop and prop[price_key] is not None]
    
    if len(values) < 4:
        logging.warning("Not enough values for IQR analysis, skipping outlier detection.")
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
    
    avg_estimated_value = avg_price_per_acre * target_acreage
    median_estimated_value = median_price_per_acre * target_acreage
    
    price_per_acre_stats = {
        "min": min(price_per_acre_values),
        "max": max(price_per_acre_values),
        "avg": avg_price_per_acre,
        "median": median_price_per_acre,
        "std_dev": statistics.stdev(price_per_acre_values) if len(price_per_acre_values) > 1 else 0
    }
    
    return {
        "estimated_value_avg": avg_estimated_value,
        "estimated_value_median": median_estimated_value,
        "price_per_acre_stats": price_per_acre_stats,
        "comparable_count": len(comparable_properties)
    }

def find_comparable_properties(
    page: WebPage, 
    latitude: float, 
    longitude: float, 
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
        
        properties = fetch_zillow_data(page, north, south, east, west)
        logging.info(f"Found {len(properties)} properties within {search_radius} miles.")
        
        filtered_properties = filter_properties_by_acreage(properties, target_acreage)
        logging.info(f"Filtered to {len(filtered_properties)} properties with compatible acreage.")
        
        all_properties.extend(filtered_properties)
        
        unique_properties = []
        addresses = set()
        for prop in all_properties:
            if prop["address"] not in addresses:
                unique_properties.append(prop)
                addresses.add(prop["address"])
        
        all_properties = unique_properties
        logging.info(f"Total unique properties found so far: {len(all_properties)}")
        
        if len(all_properties) >= MIN_COMPARABLE_PROPERTIES:
            final_radius = search_radius
            break
        
        search_radius += SEARCH_RADIUS_INCREMENT
    
    valid_properties, outlier_properties = detect_outliers_iqr(all_properties)
    
    return valid_properties, outlier_properties, final_radius

def clean_apn(apn: str) -> str:
    """
    Remove all non-numeric characters from the APN.
    Example: "456-78-901" -> "45678901"
    """
    return re.sub(r"[^0-9]", "", apn)

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Property Valuation API"}


@app.post("/test-endpoint")
async def test_endpoint():
    try:
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "API is working",
                "environment": {
                    "python_version": sys.version,
                    "platform": sys.platform,
                    "timestamp": str(datetime.now())
                }
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error_type": type(e).__name__, 
                "error_message": str(e),
                "traceback": traceback.format_exc()
            }
        )

@app.post("/valuate-property", response_model=None)  
async def valuate_property(property_request: PropertyRequest):
    request_id = str(uuid.uuid4())
    cleaned_apn = clean_apn(property_request.apn)
    page = initialize_webpage()
    logging.info(f"Request {request_id} - Browser initialized")
    logging.info(f"Request {request_id} - Cleaned APN: {cleaned_apn}")
        
    logging.info(f"Request {request_id} started - Input: {property_request.dict()}")
    
    try:
        # Log the start of processing
        logging.info(f"Starting property valuation for APN: {cleaned_apn}")
        logging.info(f"Python version: {sys.version}")
        logging.info(f"Operating system: {sys.platform}")
        
        login_to_propstream(page, EMAIL, PASSWORD)
        logging.info("Login successful")
        
        search_property(page, ADDRESS_FORMAT, cleaned_apn, property_request.county, property_request.state)
        logging.info("Property search completed")
        
        target_property_info = extract_property_info(page)
        logging.info(f"Extracted property info: {target_property_info}")
        
        target_acreage = target_property_info.get("acreage", 1.0) if target_property_info else 1.0
        
        coordinates = extract_coordinates(page.html)
        if not coordinates:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "error",
                    "request_id": request_id,
                    "error": "Property coordinates not found",
                    "property": property_request.dict()
                }
            )
        
        logging.info(f"Found coordinates: {coordinates}")
        
        latitude, longitude = coordinates
        valid_properties, outlier_properties, final_radius = find_comparable_properties(
            page, latitude, longitude, target_acreage
        )
        
        valuation_results = calculate_property_value(target_acreage, valid_properties)
        logging.info(f"Valuation completed successfully: {valuation_results}")
        
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "request_id": request_id,
                "target_property": f"APN# {cleaned_apn}, {property_request.county}, {property_request.state}",
                "target_acreage": target_acreage,
                "search_radius_miles": final_radius,
                "comparable_count": valuation_results['comparable_count'],
                "estimated_value_avg": valuation_results['estimated_value_avg'],
                "estimated_value_median": valuation_results['estimated_value_median'],
                "price_per_acre_stats": valuation_results['price_per_acre_stats'],
                "comparable_properties": valid_properties,
                "outlier_properties": outlier_properties
            }
        )
        
    except Exception as e:
        error_detail = {
            "status": "error",
            "request_id": request_id,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
            "property": property_request.dict()
        }
        logging.error(f"Request {request_id} failed: {error_detail}")
        return JSONResponse(
            status_code=500,
            content=error_detail
        )

    finally:
        page.close()
        page.quit()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

