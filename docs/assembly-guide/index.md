---
title: BBG TCG Card Sorter — Assembly Guide
---

# Bearly Board Gaming — Trading Card Game Sorter STL Assembly Guide

![Assembled sorter overview](media/image1.jpg)

## Table of Contents

- [Printing Orientation Notes](#printing-orientation-notes)
- [Base – Outer](#base--outer)
- [Base – Inner](#base--inner)
- [Scanner](#scanner)
- [Flappers (Repeat x9)](#flappers-repeat-x9)
- [Cross Braces (Repeat x2)](#cross-braces-repeat-x2)
- [Raspberry Pi Mount](#raspberry-pi-mount)
- [Tray](#tray)
- [Tray Guides](#tray-guides)
  - [Tray Guide Reference](#tray-guide-reference)
  - [Guide 0](#guide-0) · [Guide 1](#guide-1) · [Guide 2](#guide-2) · [Guide 3](#guide-3) · [Guide 4](#guide-4) · [Guide 5](#guide-5) · [Guide 6](#guide-6) · [Guide 7](#guide-7) · [Guide 8](#guide-8)
- [Final Assembly](#final-assembly)
  - [Connecting to Skadis Boards](#connecting-to-skadis-boards)
  - [Scanner Rails & Camera Mount](#scanner-rails--camera-mount)
  - [Wiring](#wiring)
- [Extras](#extras)
  - [Cable Guides](#cable-guides)
  - [Feet](#feet)
  - [Quarter-Inch Drive Adapter](#quarter-inch-drive-adapter)
  - [Shroud](#shroud)
  - [Hopper](#hopper)

---

## Printing Orientation Notes

In most cases orient the objects so they use as little supports as possible. In the following cases a less efficient orientation must be used to prevent feeding issues:

**Base**

![Base print orientation](media/image2.jpg)

**Scanner**

![Scanner print orientation](media/image3.jpg)

**Flappers**

![Flappers print orientation](media/image4.jpg)

## Base – Outer

1. Mount x3 '28BYJ-48 stepper motor' to 'Base – Leg Left Outer'

   ![Mounting stepper motors to Base Leg Left Outer](media/image5.jpg)

2. Connect 'Base – Leg Left Outer' to 'Base – Leg Left Mount'

   ![Connecting Base Leg Left Outer to Base Leg Left Mount](media/image6.jpg)

3. Connect 'Base – Leg Right Outer' to 'Base – Leg Right Mount'

   ![Connecting Base Leg Right Outer to Base Leg Right Mount](media/image7.jpg)

4. Connect 3x 'Joiner Shaft' to '28BYJ-48 stepper motor' (heating the motor shafts with a lighter maybe necessary)

   ![Connecting Joiner Shafts to stepper motors](media/image8.jpg)

## Base – Inner

1. Connect 'Base – Leg Left' and 'Base – Leg Right' to their matching 'Base – Leg \<SIDE\> Outer'

   ![Connecting Base Leg Left and Right to matching outer legs](media/image9.jpg)

2. Set previous pieces to the side and grab 'Base – Center'

   ![Base Center piece](media/image10.jpg)

3. Place 'Base – Insert' into 'Base – Center'

   ![Placing Base Insert into Base Center](media/image11.jpg)

4. Set 'Base – Guide' into 'Base – Center' making sure the pegs on the left and right set cleanly (sanding if necessary)

   ![Setting Base Guide into Base Center](media/image12.jpg)

5. *Do not disassemble, view for clarity.* Attach parts set aside earlier to the 'Base – Center' making sure the retaining peg is on top of 'Base – Guide'

   ![Attaching earlier parts to Base Center](media/image13.jpg)

6. Secure Lego wheels and pegs *(not pictured)* so two sets of two wheels are on the bottom and two wheels are on the top

7. Secure the side together with 'Screw – Knurled Small' and 'Screw – Flat Small'

   ![Securing the side with screws](media/image14.jpg)

8. *Hopper can be added and removed as wanted; it is omitted from this guide.* The Base section is now complete.

## Scanner

1. Take 'Scanner Base Bottom'

   ![Scanner Base Bottom piece](media/image15.jpg)

2. Connect 'Scanner Base' to 'Scanner Base Bottom'

   ![Connecting Scanner Base to Scanner Base Bottom](media/image16.jpg)

3. Take two 'LM393 photoelectric break-beam sensors' to the 'Scanner Base' in spots #1 and #2 — leave the screws for #2 loose for later when connecting to 'Base – Center'

   ![Placing break-beam sensors in Scanner Base](media/image17.jpg)

4. Take an SG90 servo motor, running the cable through hole 1, making sure the motor portion of the servo motor is to the right as shown with #2

   ![Routing servo motor cable through hole 1](media/image18.jpg)

5. When connecting the servo motor you will need to connect the arm of the servo so it will be able to rotate between positions #1 & #2 (shown #2 in picture), connecting the 'Card Kicker' to the servo arm

   ![Connecting servo arm to Card Kicker](media/image19.jpg)

6. Take 'Scanner Outer – Left' and 'Scanner Outer – Right' and connect them to the 'Scanner Base Bottom', taking note of the direction of the connectors *(red)*. Then set this piece to the side.

   ![Connecting Scanner Outer Left and Right](media/image20.jpg)

## Flappers (Repeat x9)

1. Take 'Flapper # - Threaded Rod'

   ![Flapper threaded rod](media/image21.jpg)

2. Screw it into 'Flapper # - Mounting Plate'

   ![Screwing threaded rod into mounting plate](media/image22.jpg)

3. Connect the 'Flapper # - Mounting Plate' in the direction of #1 — later, when assembled, it can be pushed in direction #2 to easily remove the Flapper. Screw an SG90 servo motor into position #3 with the white arm attached as shown.

   ![Connecting Flapper Mounting Plate and servo motor](media/image23.jpg)

4. Take 3 'Flapper Inserts' (using either the snap fit as shown on the far left, or the normal version — whichever performs best for you)

   ![Flapper Inserts](media/image24.jpg)

5. Take the 'Flapper' and slide the three 'Flapper Inserts' in as shown

   ![Sliding Flapper Inserts into Flapper](media/image25.jpg)

6. Flapper 1 has an additional cut out on the right side due to the proximity of the Raspberry Pi mount plate

   ![Flapper 1 cut out detail](media/image26.jpg)

7. Connect the 'Flapper # - Mount' to the 'Flapper # - Left Outer' and 'Flapper # - Right Outer'. The outer plates are all labeled with # and L for Left and R for Right.

   ![Connecting Flapper Mount to Left and Right Outer plates](media/image27.jpg)

8. The Flapper can be installed once the whole assembly is connected to the Skadis boards, for ease.

   ![Flapper ready for installation](media/image28.jpg)

## Cross Braces (Repeat x2)

1. Take 'Cross Brace – Left' & 'Cross Brace – Right' #1 and connect them together with x2 'Cross Brace – Middle' #2, then set to the side

   ![Connecting Cross Brace Left and Right with Cross Brace Middle](media/image29.jpg)

## Raspberry Pi Mount

1. #1 'Pi Mount Left' & 'Pi Mount Right' have a long side #2 and a short side #3 — the orientation is important to match as pictured when connecting to 'Pi Mount' #4. Set to the side.

   ![Assembling the Raspberry Pi Mount](media/image30.jpg)

## Tray

1. Take the 10 'Tray' items and stack them together

   ![Stacking the 10 Tray items](media/image31.jpg)

2. Secure the stacked Trays with 4 nuts 'Screw – Knurled Large' #1

   ![Securing stacked Trays with knurled nuts](media/image32.jpg)

3. Finish the Tray by attaching 'Tray – Front', then set to the side

   ![Attaching Tray Front](media/image33.jpg)

## Tray Guides

### Tray Guide Reference

1. #1 will be used for the tray guides, #2 is only for Guide 8, and there should only be two printed

   ![Tray guide reference piece #1 and #2](media/image34.jpg)

2. The 'Center Guide – Flap' pieces are not labeled, as all 8 are identical

   ![Center Guide Flap pieces](media/image35.jpg)

3. For all tray guides, foam board pieces cut to length will be secured in the guide centers at position #1. To prevent cards from falling between the Center Guide #3 and the foam board, wrap the board in tape (blue painter's tape works well) in the direction of #2. Wrapping the entire length is recommended, since the tape won't want to stick to the print — avoid starting the tape where a card could get stuck to it (leave the start and stop at the top of the board).

   ![Wrapping foam board with tape](media/image36.jpg)

### Guide 0

![Guide 0 reference](media/image37.jpg)

1. Take #1 'Left Guide – 0' and #2 'Right Guide – 0'

   ![Left Guide 0 and Right Guide 0](media/image38.jpg)

2. Secure pieces 1 & 2 from the previous step to 3 'Center Guide – 0' using screws #1 from step [Tray Guide Reference](#tray-guide-reference) through holes 4 on both sides, then place to the side

   ![Securing Guide 0 pieces to Center Guide 0](media/image39.jpg)

### Guide 1

![Guide 1 reference](media/image40.jpg)

1. Take #1 'Left Guide – 1' and #2 'Right Guide – 1'

   ![Left Guide 1 and Right Guide 1](media/image41.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 1' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 1 pieces to Center Guide 1](media/image42.jpg)

### Guide 2

![Guide 2 reference](media/image43.jpg)

1. Take #1 'Left Guide – 2' and #2 'Right Guide – 2'

   ![Left Guide 2 and Right Guide 2](media/image44.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 2' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 2 pieces to Center Guide 2](media/image45.jpg)

### Guide 3

![Guide 3 reference](media/image46.jpg)

1. Take #1 'Left Guide – 3' and #2 'Right Guide – 3'

   ![Left Guide 3 and Right Guide 3](media/image47.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 3' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 3 pieces to Center Guide 3](media/image48.jpg)

### Guide 4

![Guide 4 reference](media/image49.jpg)

1. Take #1 'Left Guide – 4' and #2 'Right Guide – 4'

   ![Left Guide 4 and Right Guide 4](media/image50.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 4' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 4 pieces to Center Guide 4](media/image51.jpg)

### Guide 5

![Guide 5 reference](media/image52.jpg)

1. Take #1 'Left Guide – 5' and #2 'Right Guide – 5'

   ![Left Guide 5 and Right Guide 5](media/image53.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 5' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 5 pieces to Center Guide 5](media/image54.jpg)

### Guide 6

![Guide 6 reference](media/image55.jpg)

1. Take #1 'Left Guide – 6' and #2 'Right Guide – 6'

   ![Left Guide 6 and Right Guide 6](media/image56.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 6' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 6 pieces to Center Guide 6](media/image57.jpg)

### Guide 7

![Guide 7 reference](media/image58.jpg)

1. Take #1 'Left Guide – 7' and #2 'Right Guide – 7'

   ![Left Guide 7 and Right Guide 7](media/image59.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 7' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then place to the side

   ![Securing Guide 7 pieces to Center Guide 7](media/image60.jpg)

### Guide 8

![Guide 8 reference](media/image61.jpg)

1. Take #1 'Left Guide – 8' and #2 'Right Guide – 8'

   ![Left Guide 8 and Right Guide 8](media/image62.jpg)

2. Secure pieces 1 *(not pictured for clarity)* & 2 from the previous step to 3 'Center Guide – 8' and 4 'Center Guide – Flap' using screws #1 from [Tray Guide Reference](#tray-guide-reference) through holes 5 on both sides, then use screw #2 from the same step through hole 6 on both sides, then place to the side

   ![Securing Guide 8 pieces to Center Guide 8](media/image63.jpg)

## Final Assembly

### Connecting to Skadis Boards

1. This assumes you have taped all of the foam board cut outs to the bottom guide pieces. Foam board from Guide 0 goes into the Scanner Base, and foam board from Guides 1–8 go into Flapper bases 1–8, with Flapper Base 9 having its piece printed on Guide 8.

   ![Foam board and guide placement](media/image64.jpg)

2. It's recommended to leave the Flappers removed from the servos so you are only trying to mount the bases.

3. For final assembly, secure all the pieces to one side of the board as shown in the picture, then flip the assembly over and adjust the pegs into the correct holes so the board drops into place. It's easiest to work from the top-left corner to the bottom-right, checking the bottom as you go — it should not take force.

   ![Flipping the assembly into the Skadis board](media/image65.jpg)

### Scanner Rails & Camera Mount

1. Secure the 'Scanner Base Rail – Left' and 'Scanner Base Rail – Right' to the Scanner

2. Secure the Pi Camera and LED strip to the 'Camera Mount', then attach it to the 'Scanner Arm – Left & Right' *(having the open areas on the bottom of the arms facing outwards)*

   ![Securing Pi Camera and LED strip to Camera Mount](media/image66.jpg)

### Wiring

1. Raspberry Pi and Servo HAT shown in proper orientation to the 'Raspberry Pi Mount'

   ![Raspberry Pi and Servo HAT orientation](media/image67.jpg)

2. **Servo Motor Connectors** — Pins 0 to Scanner Servo, Pins 1 through 9 to Flapper servos 1 through 9

## Extras

### Cable Guides

There are 4 Cable Guides that can be printed to help maintain the clean appearance of the servo wires — this would be the last step in the assembly.

![Cable Guides installed](media/image68.jpg)

### Feet

Feet can be facing inwards (good for placing on carpet) or outwards (tip-over prevention). You will need to print at least 6 (though 10 recommended) for placing evenly along the bottom edge of the Skadis boards.

![Foot orientation options](media/image69.jpg)

### Quarter-Inch Drive Adapter

Available to print is an adapter to help turn the Knurled Nuts that fits on a standard ¼" drive.

![Quarter-inch drive adapter](media/image70.jpg)

### Shroud

The Shroud to cover the Raspberry Pi and cables is secured by 4x #1 screws (from the [Tray Guide Reference](#tray-guide-reference) step) to the Raspberry Pi mount.

![Shroud covering the Raspberry Pi](media/image71.jpg)

### Hopper

Hopper #1 mounted on top of the Base, and secured with x4 large nuts, provides extra capacity and additional tension on the top axle.

![Hopper mounted on the Base](media/image72.jpg)
