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
import traceback
import redis
import json
import hashlib

from bs4 import BeautifulSoup
from DrissionPage import ChromiumOptions, WebPage
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from DrissionPage.errors import BrowserConnectError
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Constants
LOGIN_URL = "https://login.propstream.com/"
ADDRESS_FORMAT = "APN# {0}, {1}, {2}"
EMAIL = "kingsdgreatest@gmail.com"
PASSWORD = "Kanayo147*"
MILES_TO_DEGREES = 1.0 / 69
INITIAL_SEARCH_RADIUS_MILES = 1.0
MAX_SEARCH_RADIUS_MILES = 5.0
SEARCH_RADIUS_INCREMENT = 0.5
MIN_COMPARABLE_PROPERTIES = 5
MIN_ACREAGE_RATIO = 0.2
MAX_ACREAGE_RATIO = 5.0
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
    target_latitude: Optional[float] = None  
    target_longitude: Optional[float] = None 
    search_radius_miles: float
    total_comparables_found: int 
    comparable_count: int
    estimated_value_avg: Optional[float]
    estimated_value_median: Optional[float]
    price_per_acre_stats: Optional[ValuationStats]
    comparable_properties: List[ComparableProperty]
    outlier_properties: List[ComparableProperty]
    search_url: Optional[str] = None  # Make this field optional with a default value


class Settings(BaseSettings):
    CACHE_EXPIRATION: int = 24 * 60 * 60  # 24 hours
    MAX_CACHE_SIZE: int = 1000
    API_RETRY_ATTEMPTS: int = 3
    
    # Add these fields that were causing the error
    regrid_api_token: str = Field(default="")
    zillow_rapid_api_key: str = Field(default="")
    
    # Or, to allow any extra fields:
    model_config = {
        "extra": "allow",
        "env_file": ".env"
    }

settings = Settings()


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

# Redis connection
redis_client = redis.Redis(host='localhost', port=6379, db=0)

def generate_cache_key(property_request: PropertyRequest) -> str:
    """
    Generate a unique cache key based on property details
    
    Args:
        property_request (PropertyRequest): Property request object
    
    Returns:
        str: Unique MD5 hash cache key
    """
    # Create a unique string representation of the request
    cache_key_str = f"{property_request.apn}_{property_request.county}_{property_request.state}"
    
    # Generate MD5 hash to create a consistent, fixed-length key
    return hashlib.md5(cache_key_str.encode()).hexdigest()

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
    co.set_argument('--disable-extensions')
    co.set_argument('--disable-setuid-sandbox')
    co.set_argument('--single-process')
    co.set_argument('--ignore-certificate-errors')
    
    # Add memory optimization
    co.set_argument('--js-flags=--max-old-space-size=512')
    co.set_argument('--memory-pressure-off')
    
    try:
        page = WebPage(chromium_options=co)
        page.set.window.max()
        return page
    except Exception as e:
        logging.error(f"Browser initialization failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Browser initialization error: {str(e)}")

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

@lru_cache(maxsize=1000)
def get_cached_state_abbreviation(state: str) -> str:
    """Cached version of state abbreviation conversion"""
    return get_state_abbreviation(state)

# Modify existing methods to use caching
def search_property(page: WebPage, address_format: str, apn: str, county: str, state: str) -> None:
    # Use cached state abbreviation
    state_abbr = get_cached_state_abbreviation(state)
    county = county.strip()
    # Check case-insensitively if it ends with 'county'
    if county.lower().endswith('county'):
        # Split the county name to remove 'county' and add it back with proper capitalization
        base_name = county[:-(len('county'))].strip()
        county = f"{base_name.title()} County"
        logging.info(f"County name modified to: {county}")
    else:
        # Add 'County' with proper capitalization
        county = f"{county.title()} County"

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
        
        # Extract lot size
        lot_size_elem = soup.find(lambda tag: tag.name == "div" and "Lot Size" in tag.text)
        if lot_size_elem:
            lot_size_text = lot_size_elem.find_next("div").text.strip()
            
            if "acres" in lot_size_text.lower():
                logging.info("Lot size is in acres format")
                acreage_match = re.search(r"([\d.]+)\s*acres", lot_size_text.lower())
                if acreage_match:
                    property_info["acreage"] = float(acreage_match.group(1))
            elif "sqft" in lot_size_text.lower() or "sq ft" in lot_size_text.lower():
                logging.info("Lot size is in square feet format")
                sqft_match = re.search(r"([\d,]+)\s*sq", lot_size_text.lower())
                if sqft_match:
                    sqft = float(sqft_match.group(1).replace(",", ""))
                    property_info["acreage"] = sqft / 43560

        # Extract estimated value - FIXED to handle N/A values
        value_elem = soup.find(lambda tag: tag.name == "div" and "Estimated Value" in tag.text)
        if value_elem:
            value_text = value_elem.find_next("div").text.strip()
            
            # Check if the value is N/A
            if "N/A" in value_text:
                logging.info("Estimated value is N/A")
                # Don't set estimated_value in this case
            else:
                value_match = re.search(r"\$?([\d,]+)", value_text)
                if value_match and value_match.group(1):
                    property_info["estimated_value"] = float(value_match.group(1).replace(",", ""))
                else:
                    logging.warning(f"Could not extract numeric value from: '{value_text}'")
        
        return property_info
    except Exception as e:
        logging.error(f"Failed to extract property info: {e}")
        logging.error(traceback.format_exc())
        return None

def calculate_bounding_box(latitude: float, longitude: float, miles: float) -> Tuple[float, float, float, float]:
    offset = miles * MILES_TO_DEGREES
    north = latitude + offset
    south = latitude - offset
    lng_offset = offset * math.cos(latitude * math.pi / 180.0)
    east = longitude + lng_offset
    west = longitude - lng_offset
    return north, south, east, west

def fetch_zillow_data(page: WebPage, north: float, south: float, east: float, west: float) -> Tuple[List[Dict], List[Dict]]:
    def get_url_for_page(page_num: Optional[int] = None) -> str:
        """
        Generate Zillow search URL with optional pagination
        
        Args:
            page_num: Optional page number for pagination. If None, no pagination is included.
            
        Returns:
            str: Zillow search URL
        """
        # Base search query state
        search_query_state = {
            "isMapVisible": True,
            "mapBounds": {
                "west": west,
                "east": east,
                "south": south,
                "north": north
            },
            "mapZoom": 14,
            "filterState": {
                "sort": {"value": "globalrelevanceex"},
                "sf": {"value": False},
                "tow": {"value": False},
                "mf": {"value": False},
                "con": {"value": False},
                "apa": {"value": False},
                "manu": {"value": False},
                "apco": {"value": False},
                "rs": {"value": True},
                "fsba": {"value": False},
                "fsbo": {"value": False},
                "nc": {"value": False},
                "cmsn": {"value": False},
                "auc": {"value": False},
                "fore": {"value": False}
            },
            "isListVisible": True
        }
        
        # Add pagination if page number is provided
        if page_num is not None:
            search_query_state["pagination"] = {"currentPage": page_num}
        
        # Convert to JSON and encode for URL
        import json
        encoded_state = json.dumps(search_query_state).replace('"', '%22').replace(' ', '').replace(':', '%3A').replace(',', '%2C').replace('{', '%7B').replace('}', '%7D')
        
        return f"https://www.zillow.com/homes/recently_sold/?searchQueryState={encoded_state}&category=SEMANTIC"

    url = "https://zillow-com1.p.rapidapi.com/searchByUrl"
    headers = {
        "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
        "x-rapidapi-key": "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390"
    }

    all_homes = []
    potential_homes = []
    current_page = 1
    total_pages = None
    
    # Get the base URL without pagination for returning to the frontend
    base_search_url = get_url_for_page()  # No page number
    
    while total_pages is None or current_page <= total_pages:
        # Get URL with pagination for API requests
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
                    zpid = property_data.get('zpid')

                    if not lot_acres or not zpid:
                        logging.info("Skipping property: Missing lot area or ZPID")
                        continue

                    if lot_area_unit == 'sqft':
                        lot_acres = float(lot_acres) / 43560

                    property_dict = {
                        "address": f"{property_data.get('streetAddress', '')}, {property_data.get('city', '')}, {property_data.get('state', '')} {property_data.get('zipcode', '')}",
                        "price": float(price) if price else None,
                        "price_text": f"${price:,.2f}" if price else "N/A",
                        "beds": str(property_data.get('bedrooms', 'N/A')),
                        "baths": str(property_data.get('bathrooms', 'N/A')),
                        "sqft": str(property_data.get('livingArea', 'N/A')),
                        "lot_size": f"{lot_acres:.2f} acres",
                        "acreage": float(lot_acres),
                        "price_per_acre": float(price) / float(lot_acres) if price and lot_acres > 0 else None,
                        "date_sold": property_data.get('dateSold'),
                        "home_type": property_data.get('homeType'),
                        "latitude": property_data.get('latitude'),
                        "longitude": property_data.get('longitude'),
                        "zpid": zpid
                    }

                    if price:
                        logging.info(f"Added property with price: {property_dict['address']}")
                        all_homes.append(property_dict)
                    else:
                        logging.info(f"Stored potential property without price: {property_dict['address']}")
                        potential_homes.append(property_dict)

                except Exception as e:
                    logging.warning(f"Error processing property data: {e}")
                    continue

            current_page += 1
            if current_page <= total_pages:
                time.sleep(5)  # Delay to avoid rate limit

        except Exception as e:
            logging.error(f"Error fetching data from Zillow API on page {current_page}: {e}")
            break

    return all_homes, potential_homes, base_search_url

API_KEYS = [
    "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390",
    # Add additional API keys here
]

def get_api_key():
    """Return a random API key from the pool"""
    return random.choice(API_KEYS)

def fetch_price_history(zpid: str, max_retries: int = 3) -> Optional[float]:
    url = "https://zillow-com1.p.rapidapi.com/property"
    params = {"zpid": zpid}
    
    for retry in range(max_retries):
        # Get a random API key for each request
        headers = {
            "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
            "x-rapidapi-key": get_api_key()
        }
    
    for retry in range(max_retries):
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 429:
                # Calculate exponential backoff with jitter
                wait_time = (2 ** retry) + random.uniform(0, 1)
                logging.info(f"Rate limit hit for ZPID {zpid}. Retrying in {wait_time:.2f} seconds...")
                time.sleep(wait_time)
                continue
                
            response.raise_for_status()
            data = response.json()
            price_history = data.get("priceHistory", [])
            for event in price_history:
                price = event.get("price")
                if price and price > 0:
                    logging.info(f"Fetched price {price} from history for ZPID {zpid}")
                    return float(price)
            logging.info(f"No valid price found in history for ZPID {zpid}")
            return None
        except Exception as e:
            if retry < max_retries - 1:
                wait_time = (2 ** retry) + random.uniform(0, 1)
                logging.warning(f"Error fetching price history for ZPID {zpid}: {e}. Retrying in {wait_time:.2f} seconds...")
                time.sleep(wait_time)
            else:
                logging.error(f"Error fetching price history for ZPID {zpid}: {e}")
                return None

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
        logging.warning("No comparable properties found for valuation.")
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
    
    if not price_per_acre_values:
        logging.warning("No valid price per acre values found.")
        return {
            "estimated_value_avg": None,
            "estimated_value_median": None,
            "price_per_acre_stats": None,
            "comparable_count": len(comparable_properties)
        }
    
    avg_price_per_acre = statistics.mean(price_per_acre_values)
    
    # Use mean for median if only one value is available
    if len(price_per_acre_values) == 1:
        median_price_per_acre = price_per_acre_values[0]
    else:
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
) -> Tuple[List[Dict], List[Dict], float, int, str]:
    search_radius = INITIAL_SEARCH_RADIUS_MILES
    all_homes = []
    potential_homes = []
    final_radius = search_radius
    search_url = ""

    while search_radius <= MAX_SEARCH_RADIUS_MILES:
        north, south, east, west = calculate_bounding_box(latitude, longitude, search_radius)
        homes_with_price, homes_without_price, current_search_url = fetch_zillow_data(page, north, south, east, west)
        
        # Save the search URL from the first successful fetch
        if not search_url and current_search_url:
            search_url = current_search_url
        
        all_homes.extend(homes_with_price)
        potential_homes.extend(homes_without_price)

        # Deduplicate based on address
        all_homes = list({prop['address']: prop for prop in all_homes}.values())
        potential_homes = list({prop['address']: prop for prop in potential_homes}.values())

        filtered_homes = filter_properties_by_acreage(all_homes, target_acreage)
        if len(filtered_homes) >= MIN_COMPARABLE_PROPERTIES:
            final_radius = search_radius
            break

        search_radius += SEARCH_RADIUS_INCREMENT

    final_radius = search_radius if search_radius <= MAX_SEARCH_RADIUS_MILES else MAX_SEARCH_RADIUS_MILES
    filtered_homes = filter_properties_by_acreage(all_homes, target_acreage)
    potential_filtered = filter_properties_by_acreage(potential_homes, target_acreage)

    total_comparables_found = len(filtered_homes) + len(potential_filtered)

    # Fetch price history if not enough comparable properties
    if len(filtered_homes) < MIN_COMPARABLE_PROPERTIES:
        for prop in potential_filtered:
            if len(filtered_homes) >= MIN_COMPARABLE_PROPERTIES:
                break
            price = fetch_price_history(prop['zpid'])
            if price:
                prop['price'] = price
                prop['price_text'] = f"${price:,.2f}"
                prop['price_per_acre'] = price / prop['acreage'] if prop['acreage'] > 0 else None
                filtered_homes.append(prop)
                logging.info(f"Added property with price from history: {prop['address']}")
            
            # Add a longer delay between API calls to avoid rate limiting
            time.sleep(random.uniform(3, 5))  # Random delay between 3-5 seconds

    valid_properties, outlier_properties = detect_outliers_iqr(filtered_homes)

    if len(filtered_homes) < 4:
        valid_properties = filtered_homes
        outlier_properties = []

    logging.info(f"Final result: {len(valid_properties)} valid properties and {len(outlier_properties)} outliers")
    
    return valid_properties, outlier_properties, final_radius, total_comparables_found, search_url

def clean_apn(apn: str) -> str:
    """
    Remove all non-numeric characters from the APN.
    Example: "456-78-901" -> "45678901"
    """
    return re.sub(r"[^a-zA-Z0-9]", "", apn)

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Property Valuation API"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": time.time()}


@app.post("/valuate-property", response_model=ValuationResponse)
async def valuate_property(property_request: PropertyRequest):
    cleaned_apn = clean_apn(property_request.apn)

    # Generate cache key
    cache_key = generate_cache_key(property_request)
    
    # Check cache first
    cached_result = redis_client.get(cache_key)
    if cached_result:
        logging.info(f"Cache hit for {cache_key}")
        try:
            # Parse the cached data
            cached_data = json.loads(cached_result)
            # Add any missing fields with default values
            if 'search_url' not in cached_data:
                cached_data['search_url'] = None
            return ValuationResponse(**cached_data)
        except Exception as e:
            logging.warning(f"Error parsing cached data: {str(e)}. Proceeding with fresh valuation.")
            # Continue with the rest of the function if there's an error with the cached data
    
    page = initialize_webpage()
    
    try:
        # Log the start of processing
        
        login_to_propstream(page, EMAIL, PASSWORD)
        logging.info("Login successful")
        
        search_property(page, ADDRESS_FORMAT, cleaned_apn, property_request.county, property_request.state)
        logging.info("Property search completed")
        
        target_property_info = extract_property_info(page)
        
        target_acreage = target_property_info.get("acreage", 1.0) if target_property_info else 1.0
        
        coordinates = extract_coordinates(page.html)
        if not coordinates:
            raise HTTPException(status_code=404, detail="Property coordinates not found")
                
        latitude, longitude = coordinates
        valid_properties, outlier_properties, final_radius, total_comparables_found, search_url = find_comparable_properties(
            page, latitude, longitude, target_acreage
        )
        
        valuation_results = calculate_property_value(target_acreage, valid_properties)
        
        valuation_response = ValuationResponse(
            target_property=f"APN# {cleaned_apn}, {property_request.county}, {property_request.state}",
            target_acreage=target_acreage,
            target_latitude=latitude,  
            target_longitude=longitude,
            search_radius_miles=final_radius,
            total_comparables_found=total_comparables_found, 
            comparable_count=valuation_results['comparable_count'],
            estimated_value_avg=valuation_results['estimated_value_avg'],
            estimated_value_median=valuation_results['estimated_value_median'],
            price_per_acre_stats=ValuationStats(**valuation_results['price_per_acre_stats']) if valuation_results['price_per_acre_stats'] else None,
            comparable_properties=[ComparableProperty(**prop) for prop in valid_properties],
            outlier_properties=[ComparableProperty(**prop) for prop in outlier_properties],
            search_url=search_url  
        )

        # Cache the result for future use (e.g., 24 hours)
        redis_client.setex(
            cache_key, 
            24 * 60 * 60,  # 24 hours in seconds
            json.dumps(valuation_response.model_dump())
        )
        
        return valuation_response
    
    except Exception as e:
        error_trace = traceback.format_exc()
        logging.error(f"Detailed error trace:\n{error_trace}")
        logging.error(f"Error during property valuation: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "trace": error_trace,
                "step": "Property valuation process"
            }
        )
    finally:
        page.close()
        page.quit()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

