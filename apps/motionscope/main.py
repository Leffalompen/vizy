import os
import math
from threading import Thread, RLock
import kritter
import cv2
import numpy as np
import time
import json
import collections
from dash_devices.dependencies import Input, Output, State
import dash_core_components as dcc
import dash_bootstrap_components as dbc
import dash_html_components as html
from vizy import Vizy
from camera import Camera 
from capture import Capture
from process import Process
from analyze import Analyze
from tab import Tab
from motionscope_consts import WIDTH, PADDING, GRAPHS, UPDATE_RATE, BG_CNT_FINAL

"""
todo:

figure out color scheme and whether to use card for tabs

vizyvisor nav -- include info button, navbar style in app.info (including no navbar)
Draw shape -- play, record, pause in capture instead of displaying message
One video mode for camera
get rid of make_divisible, calc_video_resolution, MAX_AREA.  Push that into Kvideo as default sizing logic.  Also fix max_width for KvideoComponent while we're at it, and tab controls
to reflect the max_width (instead of 640). 
Turn off camera streaming when processing, playing, analyzing.  
Create consts file for values that are rarely used

Thumbnail image for motionscope and birdfeeder
Constant bitrate for encoder


testing:
test null case (no motion)
test short vid < BG_CNT_FINAL frames
transitions -- load file while processing, move to capture while processing (then back again)

documentation:
data:
0 pts
1 index
2 x centroid
3 y centroid
4 rect-x
5 rect-y
6 rect-width
7 rect-height

"""


APP_DIR = os.path.dirname(os.path.realpath(__file__))
MEDIA_DIR = os.path.join(APP_DIR, "media")



# Do a nested dictionary update
def deep_update(d1, d2):
    if all((isinstance(d, dict) for d in (d1, d2))):
        for k, v in d2.items():
            d1[k] = deep_update(d1.get(k), v)
        return d1
    return d2

class MotionScope:

    def __init__(self):
        if not os.path.isdir(MEDIA_DIR):
            os.system(f"mkdir -p {MEDIA_DIR}")
        self.data = collections.defaultdict(dict)
        self.kapp = Vizy()

        # Create and start camera.
        self.camera = kritter.Camera(hflip=True, vflip=True)
        self.camera.mode = "768x432x10bpp"

        style = {"label_width": 3, "control_width": 6}
        # Set video width to dynamically scale with width of window or WIDTH, whichever
        # is less.  We subtract 2*PADDING because it's on both sides. 
        self.video = kritter.Kvideo(overlay=True, video_style={"width": f"min(calc(100vw - {2*PADDING}px), {WIDTH}px)"})

        self.camera_tab = Camera(self.kapp, self.data, self.camera, self.video)
        self.capture_tab = Capture(self.kapp, self.data, self.camera)
        self.process_tab = Process(self.kapp, self.data, self.camera)
        self.analyze_tab = Analyze(self.kapp, self.data, self.video, GRAPHS)
        self.tabs = [(self.camera_tab, self.kapp.new_id()),  (self.capture_tab, self.kapp.new_id()), (self.process_tab, self.kapp.new_id()), (self.analyze_tab, self.kapp.new_id())]
        self.tab = self.camera_tab

        self.file_options = [dbc.DropdownMenuItem("Save", disabled=True), dbc.DropdownMenuItem("Load")]
        self.file_menu = kritter.KdropdownMenu(name="File", options=self.file_options, nav=True)

        nav_items = [dbc.NavItem(dbc.NavLink(p[0].name, active=i==0, id=p[1], disabled=p[0].name=="Process" or p[0].name=="Analyze")) for i, p in enumerate(self.tabs)]
        nav_items.append(self.file_menu.control)
        nav = dbc.Nav(nav_items, pills=True, navbar=True)
        navbar = dbc.Navbar(nav, color="dark", dark=True, expand=True)

        self.save_progress_dialog = kritter.KprogressDialog(title="Saving...", shared=True)
        self.load_progress_dialog = kritter.KprogressDialog(title="Loading...", shared=True)

        controls_layout = html.Div([
            # Navbar stays fixed at top
            navbar, 
            # Everything else scrolls.
            html.Div([
                html.Div([
                    html.Div([self.video, 
                        dbc.Card([t[0].layout for t in self.tabs], 
                            style={"max-width": f"{WIDTH}px", "margin-top": "10px", "margin-bottom": "10px"}
                        )
                    ], style={"float": "left"}), 
                    html.Div(self.analyze_tab.graphs.layout)
                ], style={"padding": f"{PADDING}px"})
            # Next Div is scrollable, occupies all of available viewport.    
            ], style={"overflow": "overlay"})
        # Outermost Div is flexbox 
        ], style={"display": "flex", "height": "100%", "flex-direction": "column"})

        self.kapp.layout = [controls_layout, self.save_progress_dialog, self.load_progress_dialog]

        @self.file_menu.callback()
        def func(val):
            self.run_progress = True
            if val==0:
                Thread(target=self.save_load_progress, args=(self.save_progress_dialog, )).start()
                self.data['recording'].save(os.path.join(MEDIA_DIR, "out.raw"))
            elif val==1:
                self.data['recording'] = self.camera.stream(False)
                Thread(target=self.save_load_progress, args=(self.load_progress_dialog, )).start()
                self.data['recording'].load(os.path.join(MEDIA_DIR, "out.raw"))
            self.run_progress = False

        for t in self.tabs:
            func = self.get_tab_func(t)
            self.kapp.callback_shared(None, [Input(t[1], "n_clicks")])(func)
        
        @self.capture_tab.data_update_callback
        def func(changed, cmem):
            return self.data_update(changed, cmem)

        @self.process_tab.data_update_callback
        def func(changed, cmem):
            return self.data_update(changed, cmem)

        # Run main gui thread.
        self.run_thread = True
        Thread(target=self.thread).start()

        # Run Kritter server, which blocks.
        self.kapp.run()
        self.run_thread = False

    def get_tab_func(self, tab):
        def func(val):
            mods = [Output(t[0].layout.id, "is_open", t is tab) for t in self.tabs] + [Output(t[1], "active", t is tab) for t in self.tabs]
            res = self.tab.focus(False)
            if res:
                mods += res
            self.tab = tab[0]
            res = self.tab.focus(True)
            if res:
                mods += res
            return mods 
        return func

    def data_update(self, changed, cmem=None):
        mods = []
        for t, _ in self.tabs:
            mods += t.data_update(changed, cmem)
        if "recording" in changed:
            if self.data['recording'].len()>BG_CNT_FINAL: 
                process_tab = self.find_tab("Process") 
                self.file_options[0].disabled = False
                mods += self.file_menu.out_options(self.file_options) + [Output(process_tab[1], "disabled", False)]
        if "obj_data" in changed:
            analyze_tab = self.find_tab("Analyze") 
            if self.data['obj_data']:
                f = self.get_tab_func(analyze_tab)
                mods += [Output(analyze_tab[1], "disabled", False)] + f(None)
            else: 
                mods += [Output(analyze_tab[1], "disabled", True)]

        return mods           

    def find_tab(self, name):
        for t in self.tabs:
            if t[0].name==name:
                return t 
        raise RuntimeError()

    def save_load_progress(self, dialog):
        self.kapp.push_mods(dialog.out_progress(0) + dialog.out_open(True)) 

        # Update progress while file is being saved/loaded.
        while self.run_progress:
            progress = self.data['recording'].progress()
            self.kapp.push_mods(dialog.out_progress(progress))
            time.sleep(1/UPDATE_RATE)

        mods = []
        # Save/load rest of data.
        filename = os.path.join(MEDIA_DIR, "out.json")
        # Save
        if dialog is self.save_progress_dialog: 
            with open(filename, 'w') as f:
                data = self.data.copy()
                # We don't need bg, and recording is already saved.
                del data['bg'], data['recording'] 
                json.dump(data, f, cls=kritter.JSONEncodeFromNumpy) 
        # Load        
        else: 
            # Inform tabs that we have a recording.
            mods += self.data_update("recording")
            try:
                with open(filename) as f:
                    data = json.load(f, cls=kritter.JSONDecodeToNumpy)
                deep_update(self.data, data)

                # Inform tabs that we have a list of changed
                changed = list(data.keys())
                mods += self.data_update(changed)
            except Exception as e:
                print(f"Error loading: {e}")

        #self.kapp.push_mods(dialog.out_progress(100))     
        self.kapp.push_mods(mods + dialog.out_open(False))

    def thread(self):

        while self.run_thread:
            # Get frame
            frame = self.tab.frame()
            # Send frame
            if isinstance(frame, tuple): 
                # Capture can send frameperiod with frame 
                # so it renders correctly
                self.video.push_frame(*frame)
            else:
                self.video.push_frame(frame)


if __name__ == "__main__":
    ms = MotionScope()