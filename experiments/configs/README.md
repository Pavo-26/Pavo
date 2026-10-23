# Local configuration

Copy the required templates, replace `SET_` placeholders and set their paths
in the runtime configuration. Save as UTF-8.

| Template | Settings |
|---|---|
| [pixel6_apps.example.json](pixel6_apps.example.json) | App targets and return navigation |
| [pixel6_game.example.json](pixel6_game.example.json) | Game graphics, controls and scene regions |
| [oneplus15_device.example.json](oneplus15_device.example.json) | Device serial and display size |
| [oneplus15_apps.example.json](oneplus15_apps.example.json) | Targets, selectors, coordinates and waits |
| [perfdog_ui.example.json](perfdog_ui.example.json) | PerfDog window and click positions |

Choose a followed account/video for Douyin and Kuaishou, a search-history
product keyword for Taobao, a profile for Weibo, and a downloaded iQIYI video.
Keep content fixed across runs. Pixel `return_steps` should return to the
in-App entry page. OnePlus `target_video.index` is zero-based.

The Pixel layout is 1080 × 2400. The OnePlus layout is 1272 × 2772 at 560 dpi.
The OnePlus templates use Douyin 31.7.2, Kuaishou 14.6.50.49423,
iQIYI 17.7.5, Taobao 10.64.10 and Weibo 16.7.3.
Adjust selectors and coordinates for the installed versions.

PerfDog 11.2.250408 uses the supplied 3840 × 2160 desktop layout.
Adjust the window bounds and eight click positions.
Before starting, select the phone used by the ADB connection. Game setup is in the [Pixel guide](../pixel6/README.md).
