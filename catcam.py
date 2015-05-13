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

logger = logging.getLogger('catcierge-cam')
level = logging.getLevelName('INFO')
logger.setLevel(level)

class DetectMotion(picamera.array.PiMotionAnalysis):
    def __init__(self, *args, **kwargs):
        super(DetectMotion, self).__init__(*args, **kwargs)
        self.motion = False
        self.motion_timeout = None

    def analyse(self, a):
        if self.motion_timeout and (time.time() - self.motion_timeout) > 2:
            self.motion_timeout = None

        if self.motion_timeout:
            return

        a = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)

        # If there're more than 10 vectors with a magnitude greater
        # than 60, then say we've detected motion
        if (a > 60).sum() > 5:
            print("  Motion")
            self.motion = True
            self.motion_timeout = time.time()
        else:
            self.motion = False

sigint_handlers = []
running = True
sigint_count = 0

def sighandler(signum, frame):
    global sigint_count
    print("Program: Received SIGINT, shutting down...")

    if sigint_count > 1:
        sys.exit(0)

    for handler in sigint_handlers:
        handler(signum, frame)

    sigint_count += 1
    running = False

# Trap keyboard interrupts.
signal.signal(signal.SIGINT, sighandler)
signal.signal(signal.SIGTERM, sighandler)


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

            print("Connecting ZMQ socket: %s" % connect_str)
            self.zmq_sock.connect(connect_str)

    def zmq_on_recv(self, req_topic, msg):
        """
        Receives ZMQ subscription messages from Catcierge and
        passes them on to the Websocket connection.
        """
        #req_topic = msg[0]

        if (req_topic == self.args.topic):
            self.cam_triggered = True
            self.catcierge_id = json.loads(msg)["id"]
            print('Catcierge topic %s, id: %s: Trigger cam' % (req_topic, self.catcierge_id[6:]))
        else:
            print("Catcierge topic %s: Not listening to topic" % req_topic)

    def write_video(self, stream):
        # Write the entire content of the circular buffer to disk. No need to
        # lock the stream here as we're definitely not writing to it
        # simultaneously
        with io.open('catcierge-%s-01.h264' % (self.catcierge_id), 'wb') as output:
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

    def run(self):
        global running

        with picamera.PiCamera() as camera:
            with DetectMotion(camera) as output:
                camera.start_preview()
                camera.resolution = (self.args.width, self.args.height)
                stream = picamera.PiCameraCircularIO(camera, seconds=self.args.buffer)
                camera.start_recording(stream, format='h264', motion_output=output)

                try:
                    while running:
                        camera.wait_recording(1)
                        if self.cam_triggered:
                            self.cam_triggered = False
                            print("  Camera triggered")

                            # As soon as we detect motion, split the recording to
                            # record the frames "after" motion
                            camera.split_recording(
                                "catcierge-%s-02.h264" % self.catcierge_id,
                                motion_output=output)

                            # Write the 10 seconds "before" motion to disk as well
                            self.write_video(stream)

                            # TODO: Timeout the recording after self.args.max_duration
                            # TODO: Keep recording a while even after no motion.

                            # Wait until motion is no longer detected, then split
                            # recording back to the in-memory circular buffer
                            camera.wait_recording(1)
                            while output.motion:
                                print("  Recording ...")
                                # TODO:
                                camera.wait_recording(1)

                            print("  Camera stopped")
                            camera.split_recording(stream)

                        # Check for catcierge trigger on ZMQ sub socket.
                        socks = dict(self.zpoll.poll())

                        if (self.zmq_sock in socks) and (socks[self.zmq_sock] == zmq.POLLIN):
                            topic, msg = self.zmq_sock.recv_multipart()
                            self.zmq_on_recv(topic, msg)
                finally:
                    print("Camera finally stopped")
                    camera.stop_recording()

def main():
    # TODO: Add support for combining the before and after videos.
    # TODO: Add support for uploading the video
    # https://developers.google.com/youtube/v3/code_samples/python#upload_a_video
    parser = argparse.ArgumentParser()

    parser.add_argument("--width", "-w", type=int, default=1280,
        help="Camera resolution width.")
    parser.add_argument("--height", type=int, default=720,
        help="Camera resolution height.")
    parser.add_argument("--buffer", "-b", type=int, default=10,
        help="""Circular buffer duration in seconds.
                Time to record before being triggered.""")
    parser.add_argument("--server", "-s", required=True,
        help="Catcierge ZMQ publish server hostname.")
    parser.add_argument("--port", "-p", type=int, default=5556,
        help="Catcierge ZMQ publish server port.")
    parser.add_argument("--transport", "-t", default="tcp",
        help="Catcierge ZMQ publish server transport.")
    parser.add_argument("--topic", default="",
        help="Catcierge ZMQ publish topic to listen to.")
    parser.add_argument("--max_duration", type=int, default=60,
        help="Max duration in seconds to record after triggered.")

    args = parser.parse_args()
    CatciergeCam(args).run()    

if __name__ == '__main__': sys.exit(main())
