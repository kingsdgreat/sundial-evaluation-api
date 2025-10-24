# Sundial Property Valuation API

A FastAPI-based service for real estate property valuation using comparable properties from Zillow and property data from Regrid API.

## Features

- Property valuation based on comparable sales
- Integration with Regrid API for property data
- Integration with Zillow API for comparable properties
- Redis caching for improved performance
- Rate limiting and session management
- Docker support

## Setup

### 1. Environment Variables

Create a `.env` file in the project root with the following variables:

```bash
# Regrid API Configuration
REGRID_API_TOKEN=your_regrid_api_token_here

# Zillow RapidAPI Configuration  
ZILLOW_RAPID_API_KEY=your_zillow_rapid_api_key_here

# Redis Configuration
REDIS_URL=redis://redis:6379/0

# Other Configuration
CACHE_EXPIRATION=86400
MAX_CACHE_SIZE=1000
API_RETRY_ATTEMPTS=3
```

You can use the `env_template.txt` file as a starting point.

### 2. API Keys

- **Regrid API**: Get your token from [https://app.regrid.com/](https://app.regrid.com/)
- **Zillow RapidAPI**: Get your key from [https://rapidapi.com/](https://rapidapi.com/)

### 3. Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py
```

### 4. Docker

```bash
# Build and run with Docker Compose
docker-compose up --build
```

## API Endpoints

- `GET /` - Welcome message
- `GET /health` - Health check
- `POST /valuate-property` - Property valuation endpoint

## Usage

Send a POST request to `/valuate-property` with:

```json
{
  "apn": "123456789",
  "county": "Dallas County",
  "state": "Texas"
}
```

## Configuration

The application uses Pydantic Settings to manage configuration from environment variables. All sensitive data like API keys should be stored in the `.env` file and never committed to version control.
