#!/usr/bin/python

import io
import random
import picamera
import numpy as np
import picamera.array
import argparse
import logging
import sys
import signal
import zmq
import json
import time
from zmq.eventloop import zmqstream
import multiprocessing as mp
import subprocess

logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=logging.DEBUG)
logger = logging.getLogger('catcierge-cam')

class DetectMotion(picamera.array.PiMotionAnalysis):
    def __init__(self, motion_wait_time=10, *args, **kwargs):
        super(DetectMotion, self).__init__(*args, **kwargs)
        self.motion = False
        self.motion_timeout = None
        self.motion_wait_time = motion_wait_time

    def trigger(self):
        self.motion = True
        self.motion_timeout = time.time()

    def analyse(self, a):
        if self.motion_timeout:
            duration = (time.time() - self.motion_timeout)
            if duration > self.motion_wait_time:
                self.motion_timeout = None
                self.motion = False
                logger.info("  Motion timeout %ss" % duration)

        a = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)

        # If there're more than 10 vectors with a magnitude greater
        # than 60, then say we've detected motion
        if (a > 60).sum() > 5:
            logger.info("  Motion, waiting %ss" % self.motion_wait_time)
            self.motion = True
            self.motion_timeout = time.time()


################################################################################
#                              Youtube upload                                  #
################################################################################

import httplib
import httplib2
import os
import random
import sys
import time

from apiclient.discovery import build
from apiclient.errors import HttpError
from apiclient.http import MediaFileUpload
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
import oauth2client.tools
from oauth2client.tools import argparser, run_flow

# Explicitly tell the underlying HTTP transport library not to retry, since
# we are handling retry logic ourselves.
httplib2.RETRIES = 1

# Maximum number of times to retry before giving up.
MAX_RETRIES = 10

# Always retry when these exceptions are raised.
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, httplib.NotConnected,
  httplib.IncompleteRead, httplib.ImproperConnectionState,
  httplib.CannotSendRequest, httplib.CannotSendHeader,
  httplib.ResponseNotReady, httplib.BadStatusLine)

# Always retry when an apiclient.errors.HttpError with one of these status
# codes is raised.
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]

# The CLIENT_SECRETS_FILE variable specifies the name of a file that contains
# the OAuth 2.0 information for this application, including its client_id and
# client_secret. You can acquire an OAuth 2.0 client ID and client secret from
# the Google Developers Console at
# https://console.developers.google.com/.
# Please ensure that you have enabled the YouTube Data API for your project.
# For more information about using OAuth2 to access the YouTube Data API, see:
#   https://developers.google.com/youtube/v3/guides/authentication
# For more information about the client_secrets.json file format, see:
#   https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
CLIENT_SECRETS_FILE = "client_secrets.json"

# This OAuth 2.0 access scope allows an application to upload files to the
# authenticated user's YouTube channel, but doesn't allow other types of access.
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

# This variable defines a message to display if the CLIENT_SECRETS_FILE is
# missing.
MISSING_CLIENT_SECRETS_MESSAGE = """
WARNING: Please configure OAuth 2.0

To make this sample run you will need to populate the client_secrets.json file
found at:

   %s

with information from the Developers Console
https://console.developers.google.com/

For more information about the client_secrets.json file format, please visit:
https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
""" % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                   CLIENT_SECRETS_FILE))

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")

def get_authenticated_service(args):
    flow = flow_from_clientsecrets(CLIENT_SECRETS_FILE,
                scope=YOUTUBE_UPLOAD_SCOPE,
                message=MISSING_CLIENT_SECRETS_MESSAGE)

    # TODO: Make this settable instead.
    storage = Storage("%s-oauth2.json" % sys.argv[0])
    credentials = storage.get()

    # This will open a browser and go to a verification URL.
    # If run on a headless --noauth_local_webserver must be used.
    if credentials is None or credentials.invalid:
        credentials = run_flow(flow, storage, args)

    return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,
        http=credentials.authorize(httplib2.Http()))


def initialize_upload(youtube, file, body):
    # Call the API's videos.insert method to create and upload the video.
    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(file, chunksize=-1, resumable=True,
                                   mimetype="application/octet-stream")
    )

    resumable_upload(insert_request, max)


# This method implements an exponential backoff strategy to resume a
# failed upload.
def resumable_upload(insert_request, max_retries=MAX_RETRIES):
    response = None
    error = None
    retry = 0

    while response is None:
        try:
            logger.info("Uploading file...")
            status, response = insert_request.next_chunk()
            
            if 'id' in response:
                logger.info("Video id '%s' was successfully uploaded." % response['id'])
            else:
                logger.info("The upload failed with an unexpected response: %s" % response)
                return
        except HttpError, e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = "A retriable HTTP error %d occurred:\n%s" % (e.resp.status,
                                                                     e.content)
            else:
                raise
        except RETRIABLE_EXCEPTIONS, e:
            error = "A retriable error occurred: %s" % e

        if error is not None:
            logger.info(error)
            retry += 1

            if retry > max_retries:
                logger.info("Max retries reached %s, aborting" % max_retries)
                return

            max_sleep = 2 ** retry
            sleep_seconds = random.random() * max_sleep
            logger.info("Sleeping %f seconds and then retrying..." % sleep_seconds)
            time.sleep(sleep_seconds)


class Struct(object):
    def __init__(self, **entries): 
        self.__dict__.update(entries)


def create_catcierge_description(event):
    success = event["match_group_success"]
    direction = event["match_group_direction"]

    if direction.upper() == "IN":
        prey = "Detected {0}prey".format("no " if success else "")
    else:
        prey = "Prey detection skipped when going out"

    if "match_group_succes_count" in event:
        success_count = event["match_group_succes_count"]
    else:
        success_count = len(filter(lambda m: m["success"], event["matches"]))

    if "match_group_start" in event:
        start = event["match_group_start"]
    elif "start" in event:
        start = event["start"]
    else:
        start = "unknown"

    if "match_group_end" in event:
        end = event["match_group_end"]
    elif "end" in event:
        end = event["end"]
    else:
        end = "unknown"

    d = """
    {prey}

    {success_count} of {count} matches ok ({needed} ok matches needed to keep open)

    Status: {status}
    Direction: {direction}
    In direction: {in_direction}
    Match start: {match_start}
    Match end: {match_end}
    Description: {description}

    Version: {version}
    git hash: {git_hash}
    git tainted: {git_tainted}
    Timezone: {timezone}
    Time zone offset: {timezone_offset}
    State: {state}
    Previous state: {prev_state}

    """.format(status="OK" if success else "FAIL",
               direction=direction.upper(),
               in_direction=event["settings"]["haar_matcher"]["in_direction"],
               description=event["description"],
               match_start=start,
               match_end=end,
               prey=prey,
               success_count=success_count,
               count=event["match_group_count"],
               needed=event["settings"]["ok_matches_needed"],
               version=event["version"],
               git_hash=event["git_hash"],
               git_tainted=event["git_tainted"],
               timezone=event["timezone"],
               timezone_offset=event["timezone_utc_offset"],
               state=event["state"],
               prev_state=event["prev_state"])

    # TODO: Add individual matches also

    return d


def upload_to_youtube(catcierge_id, catcierge_event, args, before_file, after_file, out_file):
    logger.info("Uploading %s to youtube" % catcierge_id)

    if not os.path.exists(before_file):
        print("Missing: %s" % before_file)

    if not os.path.exists(after_file):
        print("Missing: %s" % after_file)    

    # avconv -i concat:"catcierge-e18fc626841d5201992c5b1c000d37913dbdfc7-01.h264|catcierge-e18fc626841d5201992c5b1c000d37913dbdfc7-02.h264" -c copy catcierge-e18fc626841d5201992c5b1c000d37913dbdfc7.h264
    # https://developers.google.com/youtube/v3/code_samples/python#upload_a_video
    try:
        logger.info("Merge files %s + %s => %s" % (before_file, after_file, out_file))
        cmd = ['avconv', '-i', 'concat:%s|%s' % (before_file, after_file), '-c', 'copy', out_file]
        print("Call: %s" % " ".join(cmd))
        ret = subprocess.check_call(cmd)
    except Exception as ex:
        logger.error("Failed to merge %s with %s. Cannot upload to youtube" % (before_file, after_file))
        return

    description = create_catcierge_description(catcierge_event)
    logger.info("Video description:\n%s" % description)

    body = dict(
        snippet=dict(
            title="Catcierge %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time())),
            description=description,
            tags=[catcierge_id, "cat", "cat door"],
            categoryId="22"
        ),
        status=dict(
            privacyStatus=args.privacy_status
        )
    )

    logger.info("Authenticate towards Youtube")
    youtube = get_authenticated_service(args)

    try:
        logger.info("Initialized Youtube upload")
        initialize_upload(youtube, out_file, body)
    except HttpError, e:
        logger.info("Youtube upload, an HTTP error %d occurred:\n%s" % (e.resp.status, e.content))
    except Exception as e:
        logger.info("Failed to upload to youtube: %s" % e)

    # TODO: Add push of video ID to database for fast lookup


class CatciergeCam(object):

    def __init__(self, args):
        self.args = args
        self.cam_triggered = False
        self.zmq_connect()

    def zmq_connect(self):
        """
        Connect to ZMQ publisher.
        """
        if not hasattr(self, "zmq_ctx"):
            self.zmq_ctx = zmq.Context()
            self.zmq_sock = self.zmq_ctx.socket(zmq.SUB)
            self.zmq_sock.setsockopt(zmq.SUBSCRIBE, b"")

            self.zpoll = zmq.Poller()
            self.zpoll.register(self.zmq_sock, zmq.POLLIN)

            connect_str = "%s://%s:%d" % (self.args.transport, self.args.server, self.args.port)

            logger.info("Connecting ZMQ socket: %s" % connect_str)
            self.zmq_sock.connect(connect_str)

    def simplify_json(self, msg):
        """
        Gets rid of unused parts of the catcierge JSON that's there
        because the catciege template system sucks.
        """
        j = json.loads(msg)

        if "matches" in j:
            j["matches"] = j["matches"][:j["match_group_count"]]

            for m in j["matches"]:
                if "steps" in m:
                    m["steps"] = m["steps"][:m["step_count"]]

        return j

    def zmq_on_recv(self, req_topic, msg):
        """
        Receives ZMQ subscription messages from Catcierge and
        passes them on to the Websocket connection.
        """
        if (req_topic == self.args.topic):
            self.cam_triggered = True
            self.catcierge_event = self.simplify_json(msg)
            self.catcierge_id = self.catcierge_event["id"]
            logger.info('Catcierge topic [%s], id: %s: Trigger cam'
                    % (req_topic, self.catcierge_id[6:]))
        else:
            logger.info("Catcierge topic [%s]: Not listening to topic" % req_topic)

    def write_video(self, stream, filename):
        # Write the entire content of the circular buffer to disk. No need to
        # lock the stream here as we're definitely not writing to it
        # simultaneously
        with io.open(filename, 'wb') as output:
            for frame in stream.frames:
                if frame.frame_type == picamera.PiVideoFrameType.sps_header:
                    stream.seek(frame.position)
                    break
            while True:
                buf = stream.read1()
                if not buf:
                    break
                output.write(buf)

        # Wipe the circular stream once we're done
        stream.seek(0)
        stream.truncate()

    def on_triggered(self, camera, stream, motion_output):
        self.cam_triggered = False
        logger.info("  Camera triggered")

        before_file = os.path.abspath('catcierge-%s-01.h264' % (self.catcierge_id))
        after_file = os.path.abspath('catcierge-%s-02.h264' % (self.catcierge_id))
        out_file = os.path.abspath('catcierge-%s.h264' % (self.catcierge_id))

        record_start = time.time()
        camera.start_preview()
        motion_output.trigger()

        # As soon as we detect motion, split the recording to
        # record the frames "after" motion
        camera.split_recording(after_file, motion_output=motion_output)

        # Write the 10 seconds "before" motion to disk as well
        before = time.time()
        self.write_video(stream, before_file)

        logger.info("  Camera wrote circular buffer video in %ss" % (time.time() - before))

        # TODO: Timeout the recording after self.args.max_duration
        # TODO: Keep recording a while even after no motion.

        # Wait until motion is no longer detected, then split
        # recording back to the in-memory circular buffer
        camera.wait_recording(1)
        while motion_output.motion:
            logger.info("  Recording ...")
            camera.wait_recording(1)

        camera.split_recording(stream)
        logger.info("  Recording stopped after %ss" % (time.time() - record_start))
        camera.stop_preview()

        # Upload to youtube.
        if self.args.youtube:
            logger.info("  Youtube upload:")
            p = mp.Process(target=upload_to_youtube,
                           args=(self.catcierge_id,
                                 self.catcierge_event,
                                 self.args,
                                 before_file,
                                 after_file,
                                 out_file))
            p.start()
        else:
            logger.info("  Youtube upload: OFF")

    def run(self):
        logger.info("Starting up...")
        with picamera.PiCamera() as camera:
            with DetectMotion(camera=camera, motion_wait_time=self.args.motion_wait) as motion_output:
                camera.resolution = (self.args.width, self.args.height)
                stream = picamera.PiCameraCircularIO(camera, seconds=self.args.buffer)
                camera.start_recording(stream, format='h264', motion_output=motion_output)

                self.running = True
                try:
                    while self.running:
                        camera.wait_recording(1)
                        if self.cam_triggered:
                            self.on_triggered(camera, stream, motion_output)

                        # Check for catcierge trigger on ZMQ sub socket.
                        socks = dict(self.zpoll.poll())

                        if (self.zmq_sock in socks) and (socks[self.zmq_sock] == zmq.POLLIN):
                            topic, msg = self.zmq_sock.recv_multipart()
                            self.zmq_on_recv(topic, msg)
                finally:
                    logger.info("Camera finally stopped")
                    camera.stop_recording()
                    camera.stop_preview()

    def stop(self):
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="""
        Catcierge Camera listens to events sent from a Catcierge cat door
        detector and triggers the Raspberry Pi Camera to record video.

        By writing the video being recorded into a circular buffer continously
        video showing what happened before the actual trigger point can be
        included as well.

        The result can then be automatically uploaded to youtube.
        """,
        epilog="",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[oauth2client.tools.argparser])

    zmq_group = parser.add_argument_group("Catcierge ZMQ PUB",
        "Settings for the ZMQ PUB port that Catcierge publishes events on.")
    zmq_group.add_argument("--server", "-s", required=True,
        help="Server hostname. This is required.")
    zmq_group.add_argument("--port", "-p", type=int, default=5556,
        help="Server port.")
    zmq_group.add_argument("--transport", "-t", default="tcp",
        help="Server transport.")
    zmq_group.add_argument("--topic", default="",
        help="Topic to listen to.")

    cam_group = parser.add_argument_group("Camera settings",
        "Settings for the camera, recording durations and the like.")
    cam_group.add_argument("--width", type=int, default=1280,
        help="Camera resolution width.")
    cam_group.add_argument("--height", type=int, default=720,
        help="Camera resolution height.")
    cam_group.add_argument("--buffer", type=int, default=10,
        help="""Circular buffer duration in seconds.
                Time to record before being triggered.""")
    cam_group.add_argument("--max_duration", type=int, default=180,
        help="Max duration in seconds to record after triggered.")
    cam_group.add_argument("--motion_wait", type=int, default=10,
        help="The time to wait after detecting motion before checking again.")

    # TODO: Add output directory.

    youtube_group = parser.add_argument_group("Youtube settings",
        """
        To setup youtube authentication you first must run the program
        with the --youtube_setup flag. Make sure you have a client_secrets.json
        file in the directory:
        https://developers.google.com/api-client-library/python/guide/aaa_client_secrets
        """)
    youtube_group.add_argument("--youtube", action="store_true",
        help="Upload to youtube.")
    youtube_group.add_argument("--privacy_status", choices=VALID_PRIVACY_STATUSES,
        default=VALID_PRIVACY_STATUSES[0], help="Video privacy status.")
    youtube_group.add_argument("--youtube_setup", action="store_true",
        help="Setup youtube upload settings.")

    args = parser.parse_args()

    if args.youtube_setup:
        args.noauth_local_webserver = True
        logger.info("Setup Youtube authentication")

        youtube = get_authenticated_service(args)
        return 0

    if not args.server:
        logger.error("You must specify a catcierge publish server")
        return -1

    catcam = CatciergeCam(args)
    
    # Setup a SIG handler.
    class sighelp:
        sigint_count = 0

    def sighandler(signum, frame):
        logger.info("Received SIGINT, shutting down...")

        if sighelp.sigint_count >= 1:
            logger.info("Force exit")
            sys.exit(0)

        sighelp.sigint_count += 1
        catcam.stop()

     # Trap keyboard interrupts.
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    catcam.run()

    return 0

if __name__ == '__main__': sys.exit(main())
