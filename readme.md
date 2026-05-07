# Fork Addition
This fork provides a forecast estimation for gas and electricity prices based on data from My Luminus. The estimated values are displayed directly in Home Assistant, making it easier to monitor expected energy costs and follow price trends from within the dashboard.
The integration also includes improved session management. This helps keep the connection with My Luminus more reliable and avoids the need to manually reload the integration when the session becomes invalid.

<img width="488" height="273" alt="image" src="https://github.com/user-attachments/assets/e96d9deb-d749-4d9e-9092-85e4810266b3" />

<img width="491" height="407" alt="image" src="https://github.com/user-attachments/assets/4863ce63-a9b7-4218-8268-4d34876fe33c" />



# My Luminus - Pricing (Unofficial add-on)
- Get gas and electricity prcing from your active contracts in My Luminus (only for variable or fixed rates).
- Adds a HA device for each EAN meter.
- Daily data refresh.
- Calculate energy consumption costs in your HA energy dashboards.

## Disclaimer
This library is provided without any warranty or support by Luminus. I do not take responsibility for any problems it may cause in all cases. Use it at your own risk.

## Installation
1. Unpack files to HA /homeassistant/ folder
2. Restart HA
3. Add "My Luminus - Pricing" integration in UI.
4. Enter your My Luminus username and password.
5. Assign EAN devices to areas

## Configure energy cost in Energy dashboard settings
Configure your grid consumption and bind the costs of consumed energy with one of the exposed Luminus pricing entities.
