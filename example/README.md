# Instructions

* Navigate to a directory containing audio files, in any format supported my ffmpeg (e.g. mp3)
* ls *.mp3 > tracklist.txt
* Then run "cdda2img c" to create a CD-DA image of those files, including a PCM stream and TOC
* You can later run "cdda2img c" to extract the TOC and WAV data from the image

