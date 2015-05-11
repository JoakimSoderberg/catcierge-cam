import io
import random
import picamera
from PIL import Image
import numpy as np
import picamera.array
import argparse
import logging
import sys

logger = logging.getLogger('catcierge-web')

class DetectMotion(picamera.array.PiMotionAnalysis):
    def __init__(self):
        self.motion = False

    def analyse(self, a):
        a = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)
        # If there're more than 10 vectors with a magnitude greater
        # than 60, then say we've detected motion
        if (a > 60).sum() > 10:
            self.motion = True
        else:
            self.motion = False

sigint_handlers = []
running = True;
sigint_count = 0

def sighandler(signum, frame):
    logger.info("Program: Received SIGINT, shutting down...")

    if sigint_count > 1:
        sys.exit(0)

    for handler in sigint_handlers:
        handler(signum, frame)

    sigint_count += 1
    running = False

# Trap keyboard interrupts.
signal.signal(signal.SIGINT, sighandler)
signal.signal(signal.SIGTERM, sighandler)


class CatciergeCam:

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
            self.zmq_stream = zmqstream.ZMQStream(self.zmq_sock, tornado.ioloop.IOLoop.instance())
            self.zmq_stream.on_recv(self.zmq_on_recv)
            self.zmq_sock.setsockopt(zmq.SUBSCRIBE, b"")

            connect_str = "%s://%s:%d" % (self.args.transport, self.args.server, self.args.port)

            logger.info("Connecting ZMQ socket: %s" % connect_str)
            self.zmq_sock.connect(connect_str)

    def zmq_on_recv(self, msg):
        """
        Receives ZMQ subscription messages from Catcierge and
        passes them on to the Websocket connection.
        """
        req_topic = msg[0]

        if (req_topic == self.args.topic):
            logger.info('Catcierge topic %s: Trigger cam' % req_topic)
            self.cam_triggered = True
            self.catcierge_id = msg["id"]
        else:
            logger.info("Catcierge topic %s: Not listening to topic" % req_topic)

    def write_video(self, stream):
        # Write the entire content of the circular buffer to disk. No need to
        # lock the stream here as we're definitely not writing to it
        # simultaneously
        with io.open('catcierge-%s-01.h264', 'wb') as output:
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
            camera.start_preview()
            camera.resolution = (self.args.width, self.args.height)
            stream = picamera.PiCameraCircularIO(camera, seconds=self.args.buffer)
            camera.start_recording(stream, format='h264')

            try:
                while running:
                    camera.wait_recording(1)

                    #if detect_motion(camera):
                    if self.cam_triggered:
                        self.cam_triggered = False

                        # As soon as we detect motion, split the recording to
                        # record the frames "after" motion
                        with DetectMotion(camera) as output:
                            camera.split_recording(
                                "catcierge-%s-02.h264" % self.catcierge_id,
                                motion_output=output)

                            # Write the 10 seconds "before" motion to disk as well
                            self.write_video(stream)

                            # Wait until motion is no longer detected, then split
                            # recording back to the in-memory circular buffer
                            while output.motion:
                                camera.wait_recording(1)

                        camera.split_recording(stream)
            finally:
                camera.stop_recording()

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--width", "-w", nargs="1", default=1280
        help="Camera resolution width.")
    parser.add_argument("--height", "-h", nargs="1", default=720
        help="Camera resolution height.")
    parser.add_argument("--buffer", "-b", nargs="1", default=10
        help="""Circular buffer duration in seconds.
                Time to record before being triggered.""")
    parser.add_argument("--server", "-s", nargs="1",
        help="Catcierge ZMQ publish server hostname.")
    parser.add_argument("--port", "-p", nargs="1", default=5556
        help="Catcierge ZMQ publish server port.")
    parser.add_argument("--transport", "-t", nargs="1", default="tcp")
        help="Catcierge ZMQ publish server transport.")
    parser.add_argument("--topic", nargs="1", default=""
        help="Catcierge ZMQ publish topic to listen to.")
    parser.add_argument("--max_duration", nargs="1", default=60
        help="Max duration in seconds to record after triggered.")

    args = parser.parse_args()
    CatciergeCam(args).run()    

if __name__ == '__main__': sys.exit(main())
