![](anet_logo.png)
# AZNet BBS - Curent Release: pre Alpha v.01A

This is the start of a replacement for CNet Pro BBS for the Amiga. With the lack of support or information with CNet, and the lack of classic hardware and emulation solutions limiting some of the features of the software, a replacement is needed. Something that will run on modern machines and still give you the great experience that Sysops and users could get with CNet on the Commodores and the Amigas. 

ANet is meant to be that solution, running on linux first and then to be ported to AROS and possibly AmigaOS, MorphOS and Mac. Primary focus will be on Linux as AROS makes use of the linux kernel and drivers. This can help speed the development along. 

Some of the features that ANet should have, and are also features in CNet:


   * A threaded message system. Just about all the other BBS packages don't support this. Or they have a system in place but don't display them in a threaded format. It's just WRONG!
   * Built in FTN support. Using JAMlib to import and export messages, it should be built in as well as have it's own tosser and scanner. 
   * Easy expansion for FTP, NNTP, Web and more. 
   * Sticky posts - SubOps can make some posts sticky for like rules or important announcements that don't need to go into the news section.
   * Rate posts - Users can rate on a scale of 1-5 stars on a post or it's replies. 

More will undoubtedly be added to the above list as it matures and progresses. Progress should be done in stages to get the system as stable as possible for release. The package will be open source so the BBS community at large can help make it better. 


## Files
———————————————————————————

bbscore.py - The main code of the BBS. Running this from a terminal will launch you into the BBS. Using a modified telnet daemon, allows access from the outside. Eventually, a whole socket routine will be written so it cal all be self contained. 

bbstext.txt - This will be the file bbscore will read from for it’s prompts. This is so it is easier to translate the board into other languages. 

bbsmenu.txt - This is the file for the menus of the BBS that will allow aliasing of commands. Currently, all commands are hard coded into the ‘main()’ function. 

modem_test.zip - A test file used by the z-modem file transfer function to test sending a single file. 

## Folders
———————————————————

users - Where all the user files will be kept. Currently it’s all just plain text, but will be switched over to a sqlite database in the future. 

MCI - multiple versions of code for an MCI interpreter language for the BBS. See the readme.txt in the folder for more info. 

news - Used for keeping news announcements for the BBS. A simple date checking is enabled, but the core BBS doesn’t log calls yet, which will be added in a future version of the alpha code of bbscore. 

news/headers - a separate folder where ANSI headers are stored that the news code will display before the news text file. 

systest - System text files are stores here, such as the welcome screen and logout screen. 

sysdata - System data files will go here. A complete list will be coming soon as the older CNet files we are using as templates are brought over. 

mteld14 - modified telnet daemon that can be used to launch the BBS on a telnet connect. 


### Release Notes
------------------------------------
Dec 1st, 2013 : pre Alpha v.01A
- Some of the "basics" are working. Users can login, get 3 tries for correct password, pressing ENTER 3 times gets you a terse message to contact SYSOP.
- Simple news display is working. Will display all news items since last call at login. But last call date is not being saved yet. That comes with logging. Will add a menu system for reading news next.
- Main Menu is still hard coded in. Some basic commands will be put in till a BBSMENU file system is fleshed out. Have some ideas, but nothing coded yet.
- User data files are still just txt files. Makes it easier to edit and test user related features. When things get further along, we'll go to sqlite db.
- New user signup is still not going to happen till I have more of the BBS fleshed out and able to accept incoming calls. In the meantime, copy/paste and edit existing user files to create new users. 

August 3rd, 2023
- Added start of text editor for BBS. 
