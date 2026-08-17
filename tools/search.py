from tavily import TavilyClient
from langchain.tools import tool
from langchain_community.tools import DuckDuckGoSearchRun
import requests
from dotenv import load_dotenv
import os

load_dotenv()

TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
api_key = os.environ.get("Open_weather_api_key")
llm = os.environ.get("LLM_MODEL")

@tool
def search(query: str) -> str:
    """
    Search the web for information related to the query.
    """
    client = TavilyClient(api_key=TAVILY_API_KEY)
    try:
        results = client.search(query)
    except Exception as e:
        return f"Tavily search failed: {e}"
    return str(results)


@tool
def web_search(query: str) -> str:
    """
    Search the web for information related to the query.
    """
    duck = DuckDuckGoSearchRun()
    try:
        return duck.run(query)
    except Exception as e:
        return f"DuckDuckGo search failed: {e}"


@tool
def weather_search(location: str) -> str:
    """
    Search the web for weather information related to the location.
    """
    if not api_key:
        return "OpenWeather API key is not set in the environment variables."
    try:
        weather_client = requests.get(
            f"http://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric",
            timeout=10,
        )
    except requests.RequestException as e:
        return f"Weather lookup failed: {e}"
    if weather_client.status_code == 200:
        data = weather_client.json()
        weather_description = data['weather'][0]['description']
        temperature = data['main']['temp']
        humidity = data['main']['humidity']
        return f"Weather in {location}: {weather_description}, Temperature: {temperature}°C, Humidity: {humidity}%"
    else:
        return f"Failed to retrieve weather data for {location}. Please check the location name and try again."

@tool
def file_operations(file_name:str, query:str) -> str:
    """
    Create a file with the given name and content.
    """
    try:
        with open(file_name, 'w') as f:
            llm_response=llm.run(query)
            f.write(llm_response)
        return f"File '{file_name}' created successfully."
    except Exception as e:
        return f"Failed to create file '{file_name}': {e}"

 
Tools = {
    "search": search,
    "web_search": web_search,
    "weather_search": weather_search,
    "file_operations": file_operations,
}

