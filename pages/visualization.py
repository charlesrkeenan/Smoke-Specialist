# Dash page - /visualization
import dash
from dash import html, dcc, callback, Input, Output, get_app
from dash.exceptions import PreventUpdate
import plotly.express as px
import plotly.graph_objects as go
from utils import get_smart, generate_iframe, generate_prompt, retrieve_current_health_conditions, get_patient_demographics
from fhirclient.models.patient import Patient
from fhirclient.models.condition import Condition
import concurrent.futures
import googlemaps
import requests
import json
import pandas as pd
from datetime import datetime, timezone, timedelta
import os
import google.generativeai as genai

dash.register_page(__name__, path='/visualization')
app = get_app()

# Define the layout
layout = html.Div(id='appcontainer', children=[
    dcc.Location(id='url'),
    html.Header(id='header', children="Smoke Specialist"),
    dcc.Loading(type='circle', children=[
        html.Div(id='columncontainer', children=[
            html.Div(id='left-column', children=[
                dcc.Markdown(id='patient-details'),
                html.H4(id='address'),
                html.Iframe(id='map-iframe', referrerPolicy="no-referrer-when-downgrade")
            ]),
            html.Div(id='right-column', children=[
                dcc.Markdown(id='gemini-response'),
                dcc.Graph(id='aqi-graph')
            ])
        ])
    ])
])

@callback(
    Output('patient-details', 'children'),
    Output('address', 'children'),
    Output('map-iframe', 'src'),
    Output('gemini-response', 'children'),
    Output('aqi-graph', 'figure'),
    Input('url', 'href')
)
def handle_callback(href):
    smart = get_smart()
    # Retrieve Patient resource and patient's Condition resources. Use ThreadPoolExecutor to run the tasks concurrently
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Submit tasks to the executor
        patient_future = executor.submit(lambda: Patient.read(rem_id=smart.patient_id, server=smart.server))
        conditions_future = executor.submit(lambda: Condition.where(struct={'patient': smart.patient_id}).perform_resources(smart.server)) 
        # Retrieve the results
        patient = patient_future.result()
        conditions = conditions_future.result()

    # Check if address is not null
    if not (hasattr(patient, 'address') and len(patient.address) != 0):
        raise PreventUpdate("No address found for the patient.")
    # Get patient demographics
    try:
        name, sex, birthday, address = get_patient_demographics(patient)
        app.logger.debug(f'Patient demographics:\n{name, sex, birthday, address}')
    except Exception as e:
        app.logger.error("An error occurred while parsing the patient's demographics", exc_info=True)
        raise PreventUpdate("Something went wrong processing the patient's demographics")
    # Get patient's health conditions
    try:
        # app.logger.debug(f'Raw Condition resources: {conditions.as_json()}')
        current_health_conditions = retrieve_current_health_conditions(conditions)
        current_health_conditions = ", ".join(condition for condition in current_health_conditions)
        all_health_conditions =[]
        for condition in conditions:
            all_health_conditions.append(condition.as_json())
        app.logger.debug(f'Current conditions detected: {current_health_conditions}')
    except Exception as e:
        app.logger.error("An error occurred while parsing the patient's Condition resources", exc_info=True)
        raise PreventUpdate("Something went wrong processing the patient's health conditions")
    
    # Retrieve latitude + longitude of patient's address / retrieve embeddable google maps iFrame
    gmaps = googlemaps.Client(key=os.getenv('GOOGLE_MAPS_API_KEY'))
    # Geocoding an address
    geocode_result = gmaps.geocode(address)
    latitude = geocode_result[0]['geometry']['location']['lat']
    print(latitude)
    longitude = geocode_result[0]['geometry']['location']['lng']
    print(longitude)
    # Get iFrame
    maps_iframe = generate_iframe(address)

    # retrieve AQI history, current conditions, and forecast, then generate figure
    aqi_results = {} # aqi_results = pd.DataFrame(columns=['Time', 'AQI'])
    current_dt = datetime.now(timezone.utc)
    app.logger.debug(f"Current datetime: {current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ')}")
    for time in ['forecast', 'currentConditions', 'history']:
        url = f'https://airquality.googleapis.com/v1/{time}:lookup?key={os.getenv('GOOGLE_MAPS_API_KEY')}'
        match time:
            case 'history': # Retrieve historical AQI
                data = {
                        "hours": 720,
                        "pageSize": 720,
                        "location": {
                            "latitude": latitude,
                            "longitude": longitude
                        }
                    }
                while True: # A while loop to handle pagination
                    response = requests.post(url, headers={'Content-Type': 'application/json'}, data=json.dumps(data))
                    for hourly_result in response.json()['hoursInfo']:
                        if 'dateTime' in hourly_result and 'indexes' in hourly_result: aqi_results.update({hourly_result['dateTime']: hourly_result['indexes'][0]['aqi']})
                    if 'nextPageToken' in response.json():
                        data.update({'pageToken': response.json()['nextPageToken']})
                    else:
                        break
            case 'currentConditions': # Retrieve current AQI
                data = {
                    "location": {
                        "latitude": latitude,
                        "longitude": longitude
                    }
                }
                response = requests.post(url, headers={'Content-Type': 'application/json'}, data=json.dumps(data))
                aqi_results.update({response.json()['dateTime']: response.json()['indexes'][0]['aqi']})
            case 'forecast': # Retrieve forecasted AQI
                data = {
                    "universalAqi": "true",
                    "location": {
                        "latitude": latitude,
                        "longitude": longitude
                    },
                    "period": {
                        "startTime": (current_dt + timedelta(hours=1)).strftime(format='%Y-%m-%dT%H:%M:%SZ'),
                        "endTime": (current_dt + timedelta(hours=96)).strftime(format='%Y-%m-%dT%H:%M:%SZ')
                    },
                }
                while True: # A while loop to handle pagination
                    response = requests.post(url, headers={'Content-Type': 'application/json'}, data=json.dumps(data))
                    for hourly_forecast in response.json()['hourlyForecasts']:
                        aqi_results.update({hourly_forecast['dateTime']: hourly_forecast['indexes'][0]['aqi']})
                    if 'nextPageToken' in response.json():
                        data.update({'pageToken': response.json()['nextPageToken']})
                    else:
                        break
    # Sort the AQI results in ascending order
    # sorted_aqi_results = dict(sorted(aqi_results.items()))

    # Generate figure
    figure = go.Figure()

    # Create the traces
    figure.add_trace(go.Scatter(
        x=[dt for dt in aqi_results.keys() if dt <= current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ')],
        y=[aqi_results[dt] for dt in aqi_results.keys() if dt <= current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ')],
        mode='lines',
        line=dict(width=2, color='black')
    ))
    figure.add_trace(go.Scatter(
        x=[dt for dt in aqi_results.keys() if dt >= current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ')],
        y=[aqi_results[dt] for dt in aqi_results.keys() if dt >= current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ')],
        mode='lines',
        line=dict(dash='dot', width=2, color='grey')
    ))
    figure.update_layout(
        title='AQI History and Forecast',
        xaxis=dict(tickformat='%Y-%m-%d'),
        showlegend=False,
        font=dict(
                size=10,
                color="black"
            ),
        title_font=dict(
            size=14,
            color='black'
        )
    )


    # Ask google gemini to make a recommendation for the patient, given their age, sex, conditions, and AQI forecast. Gemini needs to be prompted with AQI background.
    genai.configure(api_key=os.getenv('GOOGLE_GEMINI_API_KEY'))
    model = genai.GenerativeModel(os.getenv('GOOGLE_GEMINI_MODEL'))
    prompt = generate_prompt(
        patient.gender,
        patient.birthDate.isostring,
        all_health_conditions,
        current_dt.strftime(format='%Y-%m-%dT%H:%M:%SZ'),
        aqi_results
    )
    app.logger.debug(f'PROMPT: {prompt}')
    gemini_response = model.generate_content(prompt)
    print(gemini_response.text) # When designing the UI, you should convert this response to markdown. See Google documentation.

    # Render the patient's details, detected address, and AQI viz
    return (
        f"Name: {name}\nDate of Birth: {birthday}\nSex: {sex}\nCurrent health conditions: {current_health_conditions}",
        f"📍 Address: {address}",
        maps_iframe,
        gemini_response.text,
        figure
    )