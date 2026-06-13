Working with Blender 5.1

This is a lightmap tool we used for 7Kingdoms Online Server.
its not 100% but we will update the github as it updades.
Currently it dose:
Outside terrain, Semi indoor terrain.
use a clean scene (remove Camera, Cube, light)

Installation
Edit -> Prefrances -> Add-Ons
Install from Disk.
Look for rose_lightmap.py and install.

once done Look in your scene tools and press ROSE Lightmap
Direct 3ddata to your Fully extracted 3ddata foulder. (NOT VFS)
in tools guide this to where you have the tool file (pairs with this plugin)

once both are in press scan. this will scan your 3ddata file for all your maps by reading the STB files.
select the map/Zone you wish to import and press load.

once loaded press Setup Sun + Sky for outdoor then Bake Zone.
it will then show a % where the mouse it while it bakes.
This will bake DIRECTLY to the 3ddata and it will also write and convert all the LIT files
the object automaticly.

Go into the mapeditor or upload to client and test it out (Faster to see results in editor)

Most the defult settings are for standard maps and dont need to be modified.
if you do modify it press Save Map settings and it will leave a small txt file with the zone file.
it will load all the prefrences for that map when you come back to it.