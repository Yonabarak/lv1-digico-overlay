# DiGiCo Aux Send Control

A PC program to control auxiliary sends on DiGiCo consoles using the OSC (Open Sound Control) protocol.

## Features

- **Individual Control**: Precise control of individual channel-to-aux send levels
- **Matrix View**: Visual matrix interface for controlling multiple channels and aux sends simultaneously
- **OSC Monitor**: Real-time monitoring of OSC messages being sent to the console
- **Flexible Configuration**: Adjustable number of channels and aux sends
- **Fine Control**: Precise level adjustments with fine-tuning buttons

## Requirements

- Python 3.7 or higher
- DiGiCo console with OSC remote control enabled
- Network connection to the console

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

### Console Setup

1. On your DiGiCo console:
   - Navigate to Setup → External Control
   - Enable OSC remote control
   - Note the console's IP address and OSC port (default is usually 10024)
   - Configure the OSC address patterns if using custom mappings

### Program Setup

1. Launch the program:
```bash
python digico_aux_control.py
```

2. Configure connection:
   - Enter the console's IP address
   - Enter the OSC port (default: 10024)
   - Set the number of channels and aux sends you want to control
   - Click "Connect"

## Usage

### Individual Control Tab

- Select a channel and aux send number
- Use the slider to adjust the level (-60 dB to +10 dB)
- Use fine-tuning buttons for precise adjustments
- Click "Mute" to mute/unmute the aux send
- Click "Reset" to return level to 0 dB

### Matrix View Tab

- View and control multiple channels and aux sends in a grid
- Adjust levels using vertical sliders
- Use "Clear All" to set all aux sends to -∞
- Refresh the matrix view if you change channel/aux counts

### OSC Monitor Tab

- Monitor all OSC messages being sent to the console
- Enable/disable monitoring as needed
- Clear the monitor log when needed

## OSC Address Patterns

The program uses standard DiGiCo OSC address patterns:
- Level: `/ch/{channel}/mix/{aux}/level`
- On/Off: `/ch/{channel}/mix/{aux}/on`

**Note**: DiGiCo consoles allow custom OSC address mapping. If your console uses different address patterns, you may need to modify the `aux_level_pattern` and `aux_on_pattern` variables in the code.

## Troubleshooting

### Connection Issues

- Verify the console's IP address and port are correct
- Ensure the PC and console are on the same network
- Check that OSC remote control is enabled on the console
- Verify firewall settings allow UDP traffic on the specified port

### No Response from Console

- Check the OSC Monitor tab to see if messages are being sent
- Verify the OSC address patterns match your console's configuration
- Consult your DiGiCo console manual for the correct OSC command format
- Some consoles may require authentication or specific message formats

### Level Values

DiGiCo consoles may use different value ranges for levels:
- Some use linear values (0.0 to 1.0)
- Some use dB values directly
- Some use normalized values

You may need to adjust the level conversion in the `on_level_change()` method based on your console's requirements.

## Customization

To customize OSC address patterns, edit these variables in `digico_aux_control.py`:

```python
self.aux_level_pattern = "/ch/{channel}/mix/{aux}/level"
self.aux_on_pattern = "/ch/{channel}/mix/{aux}/on"
```

Replace with your console's specific address format.

## License

This program is provided as-is for controlling DiGiCo consoles via OSC.

## Support

For DiGiCo-specific OSC documentation, refer to your console's user manual or contact DiGiCo support.

