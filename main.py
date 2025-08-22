import re
import time
import logging
import random
import math
import statistics
import numpy as np
import asyncio
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
    search_url: Optional[str] = None

class Settings(BaseSettings):
    CACHE_EXPIRATION: int = 24 * 60 * 60  # 24 hours
    MAX_CACHE_SIZE: int = 1000
    API_RETRY_ATTEMPTS: int = 3
    
    regrid_api_token: str = Field(default="")
    zillow_rapid_api_key: str = Field(default="")
    
    model_config = {
        "extra": "allow",
        "env_file": ".env"
    }

settings = Settings()

# Import browser pool
import asyncio
from browser_pool import browser_pool

# Add semaphore to limit concurrent requests
REQUEST_SEMAPHORE = asyncio.Semaphore(5)  # Max 5 concurrent requests

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await browser_pool.initialize()
    yield
    # Shutdown
    await browser_pool.cleanup()

app = FastAPI(
    title="Property Valuation API",
    description="API for real estate property valuation based on comparable properties",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis connection - use service name for containerized Redis
# redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)
redis_client = redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)

# Update rate limiter to use Redis for distributed limiting
from rate_limiter import api_rate_limiter
api_rate_limiter.redis_client = redis_client

def generate_cache_key(property_request: PropertyRequest) -> str:
    cache_key_str = f"{property_request.apn}_{property_request.county}_{property_request.state}"
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
    if len(state) == 2 and state.upper() in STATE_ABBREVIATIONS.values():
        return state.upper()
    
    state_name = state.strip().title()
    abbr = STATE_ABBREVIATIONS.get(state_name)
    
    if not abbr:
        logging.info(f"State conversion: Input '{state}' -> Using as-is")
        return state.upper()
    
    logging.info(f"State conversion: Input '{state}' -> Converted to '{abbr}'")
    return abbr

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
    return get_state_abbreviation(state)

def search_property(page: WebPage, address_format: str, apn: str, county: str, state: str) -> None:
    """
    Search for a property on Propstream using the provided address details.
    """
    state_abbr = get_cached_state_abbreviation(state)
    county = county.strip()
    
    if county.lower().endswith('county'):
        base_name = county[:-(len('county'))].strip()
        county = f"{base_name.title()} County"
        logging.info(f"County name modified to: {county}")
    else:
        county = f"{county.title()} County"

    address = address_format.format(apn, county, state_abbr)
    
    max_retries = 3
    retry_delay = 7  # Increased from 5 to 7 seconds
    
    for attempt in range(max_retries):
        try:
            logging.info(f"Searching for address: {address} (Attempt {attempt + 1}/{max_retries})")
            
            # Navigate to the search page (correct URL based on the images)
            page.get("https://app.propstream.com/search")
            page.wait.load_start()
            logging.info("Navigated to search page")
            
            # Wait for document and dynamic content to load
            page.wait.doc_loaded(timeout=20)
            time.sleep(5)  # Wait for dynamic content
            
            # Log page state
            logging.debug(f"Page title: {page.title}")
            logging.debug(f"Page URL: {page.url}")
            
            # Locate search input field - based on the image, it's a prominent search bar
            search_input = page.ele('xpath://input[@placeholder="Enter County, City, Zip Code(s) or APN #"]', timeout=15)
            if not search_input:
                # Fallback to more generic selectors
                search_input = page.ele('xpath://input[@type="text" or @name="search" or contains(@class, "search") or @placeholder[contains(., "search")]]', timeout=15)
                if not search_input:
                    logging.error(f"Search input not found. Page HTML: {page.html[:2000]}...")
                    raise Exception("Search input field not found")
            
            # Clear any existing text and enter the address
            search_input.clear()
            search_input.input(address)
            logging.info("Address entered in search")
            
            # Press Enter to perform the search (this is more reliable than finding a button)
            logging.info("Pressing Enter to perform search...")
            search_input.input('\n')  # Simulate Enter key
            
            # Wait for search results to load
            page.wait.doc_loaded(timeout=20)
            time.sleep(10)  # Wait longer for search results to fully load
            
            # Based on the image, after search we should see the results page with property details
            logging.info("Looking for search results...")
            
            # Check if we're on the results page by looking for the property details panel
            # The image shows property details in the right panel with "EST. VALUE" and other info
            result_found = False
            
            # Wait a bit more for the page to fully render
            time.sleep(5)
            
            # Look for the property details panel (right side of the page)
            # The image shows this contains property information like "EST. VALUE $409,000"
            property_panel = page.ele('xpath://div[contains(@class, "property") or contains(@class, "details") or contains(@class, "panel")]', timeout=15)
            if property_panel:
                logging.info("Property details panel found")
                result_found = True
            
            # If not found, try looking for the map markers (left side of the page)
            if not result_found:
                # Look for map markers or property listings
                markers = page.eles('xpath://div[contains(@class, "marker") or contains(@class, "property-marker") or contains(@style, "position")]')
                if markers:
                    logging.info(f"Found {len(markers)} potential property markers on map")
                    # Click on the first marker
                    try:
                        markers[0].click()
                        logging.info("Clicked on property marker")
                        result_found = True
                        time.sleep(3)  # Wait for details to load
                    except Exception as e:
                        logging.warning(f"Could not click marker: {e}")
            
            # If still not found, try to find by text content in the page
            if not result_found:
                # Look for the APN number or address in the page content
                result = page.ele(f'xpath://*[contains(text(), "{apn}") or contains(text(), "{county}") or contains(text(), "{state_abbr}")]', timeout=10)
                if result:
                    try:
                        result.click()
                        logging.info("Found and clicked on property result")
                        result_found = True
                        time.sleep(3)
                    except Exception as e:
                        logging.warning(f"Could not click result: {e}")
            
            # If we still haven't found results, check if the search actually worked
            if not result_found:
                # Check if we're still on the search page or if we got results
                current_url = page.url
                logging.info(f"Current URL after search: {current_url}")
                
                # If we're still on the search page, the search might not have worked
                if "search" in current_url.lower():
                    logging.error("Still on search page - search may not have worked")
                    raise Exception("Search did not produce results")
                else:
                    logging.info("URL changed, assuming search worked")
                    result_found = True
            
            # Now look for the "Details" button to get more detailed information
            # Based on the image, there's a "Details" button in the top right of the property panel
            logging.info("Looking for Details button...")
            details_button = page.ele('xpath://button[contains(text(), "Details")]', timeout=15)
            if details_button:
                try:
                    details_button.click()
                    logging.info("Details button clicked")
                    page.wait.doc_loaded(timeout=20)
                    time.sleep(5)  # Wait for details page to load
                except Exception as e:
                    logging.warning(f"Could not click Details button: {e}")
            else:
                logging.info("Details button not found - may already be on details page")
            
            # Now we should be on the property detail page with all the information
            logging.info("Property search and details navigation completed")
            
            return
        
        except Exception as e:
            logging.warning(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                logging.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                page.refresh()
            else:
                logging.error(f"Property search failed after {max_retries} attempts: {str(e)}")
                raise HTTPException(
                    status_code=404,
                    detail=f"No search results found for APN# {apn}, {county}, {state_abbr}"
                )

def extract_coordinates(html: str) -> Optional[Tuple[float, float]]:
    soup = BeautifulSoup(html, "html.parser")
    
    # Try multiple methods to find coordinates
    
    # Method 1: Look for Google Maps links
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "maps.google.com/maps" in href and "ll=" in href:
            match = re.search(r"ll=([-+]?\d*\.\d+),([-+]?\d*\.\d+)", href)
            if match:
                try:
                    return float(match.group(1)), float(match.group(2))
                except ValueError:
                    logging.warning("Failed to convert coordinates to float.")
    
    # Method 2: Look for data attributes that might contain coordinates
    for element in soup.find_all(attrs={"data-lat": True, "data-lng": True}):
        try:
            lat = float(element.get("data-lat"))
            lng = float(element.get("data-lng"))
            return lat, lng
        except (ValueError, TypeError):
            continue
    
    # Method 3: Look for coordinates in script tags
    for script in soup.find_all("script"):
        if script.string:
            # Look for latitude/longitude patterns
            lat_match = re.search(r'"latitude":\s*([-+]?\d*\.\d+)', script.string)
            lng_match = re.search(r'"longitude":\s*([-+]?\d*\.\d+)', script.string)
            if lat_match and lng_match:
                try:
                    return float(lat_match.group(1)), float(lng_match.group(1))
                except ValueError:
                    continue
    
    # Method 4: Look for coordinates in the page URL or meta tags
    for meta in soup.find_all("meta", attrs={"name": ["geo.position", "latitude", "longitude"]}):
        content = meta.get("content", "")
        if "latitude" in meta.get("name", "").lower():
            try:
                lat = float(content)
                # Look for corresponding longitude
                for lng_meta in soup.find_all("meta", attrs={"name": "longitude"}):
                    try:
                        lng = float(lng_meta.get("content", ""))
                        return lat, lng
                    except ValueError:
                        continue
            except ValueError:
                continue
    
    logging.warning("Coordinates not found in HTML.")
    return None

def extract_property_info(page: WebPage) -> Optional[Dict]:
    try:
        html = page.html
        soup = BeautifulSoup(html, "html.parser")
        property_info = {}
        
        logging.info("Extracting property information from PropStream page...")
        
        # Based on the second image, look for property details in the right panel
        # The image shows "EST. VALUE $409,000" prominently displayed
        
        # Method 1: Look for estimated value (most prominent in the image)
        value_patterns = [
            r"EST\.?\s*VALUE\s*\$?([\d,]+)",
            r"Estimated\s+Value\s*\$?([\d,]+)",
            r"\$([\d,]+)\s*EST\.?\s*VALUE",
            r"Value:\s*\$?([\d,]+)"
        ]
        
        for pattern in value_patterns:
            value_match = re.search(pattern, html, re.IGNORECASE)
            if value_match:
                try:
                    value_str = value_match.group(1).replace(",", "")
                    property_info["estimated_value"] = float(value_str)
                    logging.info(f"Found estimated value: ${property_info['estimated_value']:,.2f}")
                    break
                except ValueError:
                    continue
        
        # Method 2: Look for lot size/acreage information
        # The image shows "10,400 LOT" which suggests lot size
        lot_patterns = [
            r"(\d+(?:,\d+)?)\s*LOT",
            r"Lot\s+Size[:\s]*([\d,]+)\s*(?:sq\s*ft|acres?)",
            r"(\d+(?:\.\d+)?)\s*acres?",
            r"(\d+(?:,\d+)?)\s*sq\s*ft"
        ]
        
        for pattern in lot_patterns:
            lot_match = re.search(pattern, html, re.IGNORECASE)
            if lot_match:
                try:
                    lot_value = lot_match.group(1).replace(",", "")
                    if "sq ft" in lot_match.group(0).lower():
                        # Convert square feet to acres
                        sqft = float(lot_value)
                        property_info["acreage"] = sqft / 43560
                        logging.info(f"Found lot size: {sqft:,.0f} sq ft ({property_info['acreage']:.2f} acres)")
                    else:
                        property_info["acreage"] = float(lot_value)
                        logging.info(f"Found acreage: {property_info['acreage']:.2f} acres")
                    break
                except ValueError:
                    continue
        
        # Method 3: Look for property details in structured elements
        # Try to find elements with specific text patterns
        for elem in soup.find_all(["div", "span", "p"]):
            text = elem.get_text().strip()
            
            # Look for lot size information
            if "lot" in text.lower() and any(char.isdigit() for char in text):
                lot_match = re.search(r"(\d+(?:,\d+)?)\s*(?:sq\s*ft|acres?)", text, re.IGNORECASE)
                if lot_match and "acreage" not in property_info:
                    try:
                        lot_value = lot_match.group(1).replace(",", "")
                        if "sq ft" in text.lower():
                            sqft = float(lot_value)
                            property_info["acreage"] = sqft / 43560
                        else:
                            property_info["acreage"] = float(lot_value)
                        logging.info(f"Found lot size from text: {text}")
                    except ValueError:
                        continue
            
            # Look for value information
            if "value" in text.lower() and "$" in text:
                value_match = re.search(r"\$([\d,]+)", text)
                if value_match and "estimated_value" not in property_info:
                    try:
                        value_str = value_match.group(1).replace(",", "")
                        property_info["estimated_value"] = float(value_str)
                        logging.info(f"Found value from text: {text}")
                    except ValueError:
                        continue
        
        # If we still don't have acreage, set a default
        if "acreage" not in property_info:
            property_info["acreage"] = 1.0
            logging.warning("No acreage found, using default value of 1.0 acres")
        
        # If we still don't have estimated value, set to None
        if "estimated_value" not in property_info:
            logging.warning("No estimated value found")
            property_info["estimated_value"] = None
        
        logging.info(f"Extracted property info: {property_info}")
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

def fetch_zillow_data(page: WebPage, north: float, south: float, east: float, west: float) -> Tuple[List[Dict], List[Dict], str]:
    def get_url_for_page(page_num: Optional[int] = None) -> str:
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
        
        if page_num is not None:
            search_query_state["pagination"] = {"currentPage": page_num}
        
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
    
    base_search_url = get_url_for_page()
    
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
                time.sleep(5)

        except Exception as e:
            logging.error(f"Error fetching data from Zillow API on page {current_page}: {e}")
            break

    return all_homes, potential_homes, base_search_url

API_KEYS = [
    "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390",
]

def get_api_key():
    return random.choice(API_KEYS)

def fetch_price_history(zpid: str, max_retries: int = 3) -> Optional[float]:
    url = "https://zillow-com1.p.rapidapi.com/property"
    params = {"zpid": zpid}
    
    for retry in range(max_retries):
        headers = {
            "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
            "x-rapidapi-key": get_api_key()
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 429:
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
        
        if not search_url and current_search_url:
            search_url = current_search_url
        
        all_homes.extend(homes_with_price)
        potential_homes.extend(homes_without_price)

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
            
            time.sleep(random.uniform(3, 5))

    valid_properties, outlier_properties = detect_outliers_iqr(filtered_homes)

    if len(filtered_homes) < 4:
        valid_properties = filtered_homes
        outlier_properties = []

    logging.info(f"Final result: {len(valid_properties)} valid properties and {len(outlier_properties)} outliers")
    
    return valid_properties, outlier_properties, final_radius, total_comparables_found, search_url

def clean_apn(apn: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", apn)

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Property Valuation API"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": time.time()}

@app.post("/valuate-property", response_model=ValuationResponse)
async def valuate_property(property_request: PropertyRequest):
    async with REQUEST_SEMAPHORE:
        return await _process_valuation(property_request)


async def _process_valuation(property_request: PropertyRequest):
    """
    Process a property valuation request, fetching data from Propstream and Zillow.
    Uses cached results if available and stores new results in Redis.
    """
    cleaned_apn = clean_apn(property_request.apn)
    cache_key = generate_cache_key(property_request)
    
    # Check Redis cache for existing valuation
    cached_result = redis_client.get(cache_key)
    if cached_result:
        logging.info(f"Cache hit for {cache_key}")
        try:
            cached_data = json.loads(cached_result)
            if 'search_url' not in cached_data:
                cached_data['search_url'] = None
            return ValuationResponse(**cached_data)
        except Exception as e:
            logging.warning(f"Error parsing cached data: {str(e)}. Proceeding with fresh valuation.")
    
    # Acquire rate limit slot
    rate_limit_key = f"valuation:{cleaned_apn}"
    if not await api_rate_limiter.acquire(rate_limit_key):
        logging.warning(f"Rate limit exceeded for {rate_limit_key}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded, please try again later")

    async with browser_pool.get_browser() as page:
        try:
            # Login to Propstream
            await asyncio.get_event_loop().run_in_executor(
                None, 
                login_to_propstream, 
                page, EMAIL, PASSWORD
            )
            logging.info("Login successful")

            # Search for the target property
            await asyncio.get_event_loop().run_in_executor(
                None,
                search_property,
                page, ADDRESS_FORMAT, cleaned_apn, 
                property_request.county, property_request.state
            )
            logging.info("Property search completed")

            # Extract property information
            target_property_info = await asyncio.get_event_loop().run_in_executor(
                None, extract_property_info, page
            )
            if not target_property_info:
                logging.error("Failed to extract target property info")
                raise HTTPException(status_code=500, detail="Failed to extract target property information")

            target_acreage = target_property_info.get("acreage", 1.0)

            # Extract coordinates
            coordinates = await asyncio.get_event_loop().run_in_executor(
                None, extract_coordinates, page.html
            )
            if not coordinates:
                logging.error("Property coordinates not found")
                raise HTTPException(status_code=404, detail="Property coordinates not found")

            latitude, longitude = coordinates

            # Fetch comparable properties from Zillow
            valid_properties, outlier_properties, final_radius, total_comparables_found, search_url = await asyncio.get_event_loop().run_in_executor(
                None, find_comparable_properties, page, latitude, longitude, target_acreage
            )

            # Calculate valuation based on comparable properties
            valuation_results = calculate_property_value(target_acreage, valid_properties)
            
            # Prepare response
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

            # Cache the response in Redis
            try:
                redis_client.setex(
                    cache_key,
                    settings.CACHE_EXPIRATION,  # Use configured cache expiration
                    json.dumps(valuation_response.model_dump())
                )
                logging.info(f"Cached valuation response for {cache_key}")
            except Exception as e:
                logging.warning(f"Failed to cache valuation response: {str(e)}")

            return valuation_response

        except HTTPException:
            raise  # Re-raise HTTP exceptions to be handled by FastAPI
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

