from __future__ import division
import os
import numpy as np
from mayavi.core.ui.api import MayaviScene, SceneEditor, MlabSceneModel
from traits.api import (Bool, Button, cached_property, File, HasTraits,
    Instance, on_trait_change, Str, Property, Directory, Dict, DelegatesTo,
    HasPrivateTraits, Any, List, Enum, Int, Event)
from traitsui.api import (View, Item, Group, OKCancelButtons, ShellEditor,
    HGroup,VGroup, InstanceEditor, TextEditor, ListEditor, CSVListEditor,
    Handler)

from electrode import Electrode
from utils import virtual_points3d, NameHolder, GeometryNameHolder
from utils import crash_if_freesurfer_is_not_sourced, gensym
from geometry import load_affine

class ElectrodePositionsModel(HasPrivateTraits):
    ct_scan = File
    t1_scan = File
    subjects_dir = Directory
    subject = Str
    fsdir_writable = Bool

    electrode_geometry = List(List(Int), [[8,8]]) # Gx2 list

    _electrodes = List(Electrode)
    interactive_mode = Instance(NameHolder)
    _grids = Dict # Grid -> List(Electrode)
    _grid_named_objects = Property(depends_on='_grids')

    _sorted_electrodes = Dict # Tuple -> Electrode
    _interpolated_electrodes = Dict # Tuple -> Electrode
    _unsorted_electrodes = Dict # Tuple -> Electrode
    _all_electrodes = Dict # Tuple -> Electrode
        # dictionary from surface coordinates (as hashable) to reused
        # electrode objects

    _points_to_cur_grid = Dict
    _points_to_unsorted = Dict

    _rebuild_vizpanel_event = Event
    _visualization_ready = Bool(False)
    _update_colors_event = Event

    _colors = Any # OrderedDict(Grid -> Color)
    _color_scheme = Any #Generator returning 3-tuples
    _grid_geom = Dict # Grid -> Gx2 list

    ct_registration = File

    @cached_property
    def _get__grid_named_objects(self):
        from color_utils import mayavi2traits_color
        grid_names = [NameHolder(name=''), 
            GeometryNameHolder(name='unsorted',
                geometry='n/a',
                #TODO dont totally hardcode this color
                color=mayavi2traits_color((1,0,0)))]

        #for key in sorted(self._grids.keys()):
        #use the canonical order as the order to appear in the list
        if self._colors is not None:
            for key in self._colors.keys():
                if key=='unsorted':
                    continue
                grid_names.append(GeometryNameHolder(
                    name=key, 
                    geometry=str(self._grid_geom[key]), 
                    color=mayavi2traits_color(self._colors[key])))

        #if len(self._grids) > 0:
        #import pdb
        #pdb.set_trace()

        return grid_names

    def _interactive_mode_changed(self):
        self._commit_grid_changes()

        self._points_to_cur_grid = {}
        self._points_to_unsorted = {}

    def _commit_grid_changes(self):
        for p in (self._points_to_cur_grid, self._points_to_unsorted):
            for loc in p:
                elec = p[loc]
                
                old = elec.grid_name
                new = elec.grid_transition_to

                elec.grid_name = new
                elec.grid_transition_to = ''
        
                if old != 'unsorted':
                    self._grids[old].remove(elec)
                if new != 'unsorted':
                    self._grids[new].append(elec)
    
    def run_pipeline(self):
        #setup
        if self.subjects_dir is None or self.subjects_dir=='':
            self.subjects_dir = os.environ['SUBJECTS_DIR']
        if self.subject is None or self.subject=='':
            self.subject = os.environ['SUBJECT']

        self.interactive_mode = self._grid_named_objects[0]

        self._electrodes = []
        self._all_electrodes = {}
        self._unsorted_electrodes = {}
        self._sorted_electrodes = {}
        self._interpolated_electrodes = {}

        self._visualization_ready = False

        #pipeline
        import pipeline as pipe
        
        ct_mask = pipe.create_brainmask_in_ctspace(self.ct_scan,
            subjects_dir=self.subjects_dir, subject=self.subject)

        self._electrodes = pipe.identify_electrodes_in_ctspace(
            self.ct_scan, mask=ct_mask) 

        if self.ct_registration not in (None, ''):
            aff = load_affine(self.ct_registration)
        else:
            aff = pipe.register_ct_to_mr_using_mutual_information(
                self.ct_scan, subjects_dir=self.subjects_dir, 
                subject=self.subject)

        pipe.create_dural_surface(subjects_dir=self.subjects_dir, 
            subject=self.subject)

        #initial sorting
        #self._grids, self._colors = pipe.classify_electrodes(
        self._colors, self._grid_geom, self._grids, self._color_scheme = (
            pipe.classify_electrodes(self._electrodes,
                                     self.electrode_geometry,
                                     delta = .5
                                    ))

        # add grid labels to electrodes
        for key in self._grids:
            for elec in self._grids[key]:
                elec.grid_name = key

        # add interpolated points to overall list
        for key in self._grids:
            for elec in self._grids[key]:
                if elec.is_interpolation:
                    self._electrodes.append(elec)

        pipe.translate_electrodes_to_surface_space(
            self._electrodes, aff, subjects_dir=self.subjects_dir,
            subject=self.subject)

        #a very rapid cooling schedule shows pretty good performance
        #additional cooling offers very marginal returns and we prioritize
        #quick results so the user can adjust them
        pipe.snap_electrodes_to_surface(
            self._electrodes, subjects_dir=self.subjects_dir,
            #subject=self.subject, max_steps=2500)
            subject=self.subject, max_steps=10)

        # Store the sorted/interpolated points in separate maps for access
        for key in self._grids:
            for elec in self._grids[key]:
                if elec.is_interpolation:
                    self._interpolated_electrodes[elec.astuple()] = elec
                else:
                    self._sorted_electrodes[elec.astuple()] = elec

        # store the unsorted points in a separate map for access
        for elec in self._electrodes:
            sorted = False
            for key in self._grids:
                if sorted:
                    break
                for elec_other in self._grids[key]:
                    if elec is elec_other:
                        sorted=True
                        break
            if not sorted:
                self._unsorted_electrodes[elec.astuple()] = elec

        self._all_electrodes.update(self._interpolated_electrodes)
        self._all_electrodes.update(self._unsorted_electrodes)
        self._all_electrodes.update(self._sorted_electrodes)
    
        self._visualization_ready = True
        self._rebuild_vizpanel_event = True

    def add_grid(self):
        name = 'usergrid%s'%gensym()

        self.interactive_mode = self._grid_named_objects[0]

        #force self._grids to update (GUI depends on cached property)
#        temp_grids = self._grids
#        self._grids = {}
#        self._grids.update(temp_grids)

        #geometry and color data should be defined first so that when grids
        #grids is updated the GUI does not error out looking for this info
        self._grid_geom[name] = 'user-defined'
        self._colors[name] = self._color_scheme.next()

        #testing GUI update bug
        #temp_grids = self._grids.copy()
        #temp_grids[name] = []
        #self._grids = temp_grids

        self._grids[name] = []
        
        self._update_colors_event = True

    def fit_changes(self):
        #maybe this should be a different call which evaluates a single
        #grid

        #currently we dont use this
        _, _, self._grids = pipe.classify_electrodes(
            self._electrodes, self.electrode_geometry,
            fixed_points=self._grids.values())

    def save_labels(self):
        #TODO run the grid fitting procedure with the complete fixed
        #set of electrodes, to determine the resultant geometry
        #then assign 2D indices based on that geometry

        from traitsui.file_dialog import open_file
        labeldir = open_file(can_create_dir=True)

        if os.path.exists(labeldir) and not os.path.isdir(labeldir):
            raise ValueError('Cannot write labels to a non-directory')

        try:
            os.makedirs(labeldir)
        except OSError:
            #potentially handle errno further
            pass

        from mne.label import Label

        #import pdb
        #pdb.set_trace()

        for key in self._grids:
            for j,elec in enumerate(self._grids[key]):
                label_name = '%s_elec%i'%(key,j)
                label = Label(vertices=[elec.vertno], 
                              pos=[elec.pial_coords.tolist()],
                              subject=self.subject, hemi=elec.hemi,
                              name=label_name)
                label.save( os.path.join( labeldir, label_name ))

#class IntermediateVizInterface(Handler):
#    viz = Instance(SurfaceVisualizerPanel)

class SurfaceVisualizerPanel(HasTraits):
    scene = Instance(MlabSceneModel,())
    model = Instance(ElectrodePositionsModel)

    subject = DelegatesTo('model')
    subjects_dir = DelegatesTo('model')
    _colors = DelegatesTo('model')

    _grids = DelegatesTo('model')
    interactive_mode = DelegatesTo('model')

    _points_to_unsorted = DelegatesTo('model')
    _points_to_cur_grid = DelegatesTo('model')

    _all_electrodes = DelegatesTo('model')
    _unsorted_electrodes = DelegatesTo('model')

    brain = Any
    gs_glyphs = Dict

    traits_view = View(
        Item('scene', editor=SceneEditor(scene_class=MayaviScene),
            show_label=False),
        height=500, width=500)

    def __init__(self, model, **kwargs):
        super(SurfaceVisualizerPanel, self).__init__(**kwargs)
        self.model = model

    @on_trait_change('scene:activated')
    def setup(self):
        if self.model._visualization_ready:
            self.show_grids_on_surface()

    def show_grids_on_surface(self):
        self.model._visualization_ready = False

        from mayavi import mlab
        #mlab.clf(figure = self.scene.mayavi_scene)

        #there is a bug in mlab.clf which causes the callbacks to become
        #disconnected in such a way that they cannot be reattached to the
        #scene. I tracked this bug to the VTK picker before giving up.

        #To avoid this, we use a workaround -- discard the scene every
        #single time we want to use mlab.clf and replace it with an
        #entirely new MlabSceneModel instance. This has the added benefit
        #of (according to my tests) avoiding memory leaks.

        from utils import clear_scene

        clear_scene(self.scene.mayavi_scene)

        from color_utils import set_discrete_lut

        import surfer
        #import pdb
        #pdb.set_trace()
        brain = self.brain = surfer.Brain( 
            self.subject, subjects_dir=self.subjects_dir,
            surf='pial', curv=False, hemi='both',
            figure=self.scene.mayavi_scene)

        brain.toggle_toolbars(True)

        unsorted_elecs = map((lambda x:getattr(x, 'snap_coords')),
            self._unsorted_electrodes.values())
        self.gs_glyphs['unsorted'] = glyph = virtual_points3d( 
            unsorted_elecs, scale_factor=0.3, name='unsorted',
            figure=self.scene.mayavi_scene, color=self._colors['unsorted'])  

        set_discrete_lut(glyph, self._colors.values())
        glyph.mlab_source.dataset.point_data.scalars=(
            np.zeros(len(unsorted_elecs)))

        for i,key in enumerate(self._grids):
            grid_elecs = map((lambda x:getattr(x, 'snap_coords')), 
                self._grids[key])

            if len(grid_elecs)==0:
                continue

            self.gs_glyphs[key] = glyph = virtual_points3d(grid_elecs,
                scale_factor=0.3, color=self._colors[key], 
                name=key, figure=self.scene.mayavi_scene)

            set_discrete_lut(glyph, self._colors.values())
            scalar_color = self._colors.keys().index(key)

            glyph.mlab_source.dataset.point_data.scalars=(
                np.ones(len(self._grids[key])) * scalar_color)

        #set the surface unpickable
        for srf in brain.brains:
            srf._geo_surf.actor.actor.pickable=False
            srf._geo_surf.actor.property.opacity = 0.4

        #setup the node selection callback
        picker = self.scene.mayavi_scene.on_mouse_pick( self.selectnode_cb )
        picker.tolerance = .02

    def selectnode_cb(self, picker):
        '''
        Callback to move an node into the selected state
        '''
        from color_utils import change_single_glyph_color
        from mayavi import mlab

        if self.interactive_mode is None:
            return
        target = self.interactive_mode.name
        if target in ('', 'unsorted'):
            return

        for key,nodes in zip(self.gs_glyphs.keys(), self.gs_glyphs.values()):
            if picker.actor in nodes.actor.actors:
                pt = int(picker.point_id/nodes.glyph.glyph_source.
                    glyph_source.output.points.to_array().shape[0])
                x,y,z = nodes.mlab_source.points[pt]
                elec = self._all_electrodes[(x,y,z)]
                current_key = elec.grid_name
                break

        #import pdb
        #pdb.set_trace()

        if elec in self._grids[target]:
            if (x,y,z) in self._points_to_unsorted:
                del self._points_to_unsorted[(x,y,z)]
                change_single_glyph_color(nodes, pt, 
                    self._colors.keys().index(current_key))
                elec.grid_transition_to = ''
            else:
                self._points_to_unsorted[(x,y,z)] = elec
                change_single_glyph_color(nodes, pt, 
                    self._colors.keys().index('unsorted'))
                elec.grid_transition_to = 'unsorted'
        else:
            if (x,y,z) in self._points_to_cur_grid:
                del self._points_to_cur_grid[(x,y,z)]
                change_single_glyph_color(nodes, pt, 
                    self._colors.keys().index(current_key))
                elec.grid_transition_to = ''
            else:
                self._points_to_cur_grid[(x,y,z)] = elec
                change_single_glyph_color(nodes, pt, 
                    self._colors.keys().index(target))
                elec.grid_transition_to = target

        mlab.draw()

    @on_trait_change('model:_update_colors_event')
    def update_colors(self):
        from color_utils import set_discrete_lut
        for glyph in self.gs_glyphs.values():
            set_discrete_lut(glyph, self._colors.values())
                
class InteractivePanel(HasPrivateTraits):
    model = Instance(ElectrodePositionsModel)

    ct_scan = DelegatesTo('model')
    t1_scan = DelegatesTo('model')
    run_pipeline_button = Button('Extract electrodes to surface')

    subjects_dir = DelegatesTo('model')
    subject = DelegatesTo('model')
    fsdir_writable = DelegatesTo('model')

    ct_registration = DelegatesTo('model')

    electrode_geometry = DelegatesTo('model')

    _grid_named_objects = DelegatesTo('model')

    #interactive_mode = Instance(NameHolder)
    interactive_mode = DelegatesTo('model')
    add_grid_button = Button('Add new grid')
    shell = Dict

    save_labels_button = Button('Save labels')

    #we retain a reference to easily reference the visualization in the shell
    viz = Instance(SurfaceVisualizerPanel)

    traits_view = View(
        HGroup(
            VGroup(
                Item('ct_scan'),
                Item('ct_registration', label='reg matrix\n(optional)')
            ),
            VGroup(
                Item('electrode_geometry', editor=ListEditor(
                    editor=CSVListEditor(), rows=2), ), 
            ), 
            VGroup(
                Item('run_pipeline_button', show_label=False),
            ),
        ),
        HGroup(
                Item('subjects_dir'),
                Item('subject'),
        ),
        HGroup(
            VGroup(
                Item('interactive_mode', 
                    editor=InstanceEditor(name='_grid_named_objects'),
                    style='custom', label='Add/remove electrodes from'),
            ),
            VGroup(
                Item('add_grid_button', show_label=False),
            ),
            VGroup(
                Item('save_labels_button', show_label=False),
            ),
        ),

                Item('shell', show_label=False, editor=ShellEditor()),
        height=300, width=500
    )

    def __init__(self, model, viz=None, **kwargs):
        super(InteractivePanel, self).__init__(**kwargs)
        self.model = model
        self.viz = viz

    def _run_pipeline_button_fired(self):
        self.model.run_pipeline()

    def _add_grid_button_fired(self):
        self.model.add_grid()

    def _find_best_fit_button_fired(self):
        self.model.fit_changes()

    def _save_labels_button_fired(self):
        self.model.save_labels()

class iEEGCoregistrationFrame(HasTraits):
    model = Instance(ElectrodePositionsModel)
    interactive_panel = Instance(InteractivePanel)
    surface_visualizer_panel = Instance(SurfaceVisualizerPanel)
    #viz_interface = Instance(IntermediateVizInterface)

    traits_view = View(
        Group(
            Item('surface_visualizer_panel', editor=InstanceEditor(), 
                style='custom', resizable=True ),
            Item('interactive_panel', editor=InstanceEditor(), style='custom',
                resizable=True),
        show_labels=False),

        title=('llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch is'
            ' nice this time of year'),
        height=800, width=700, resizable=True
    )

    def __init__(self, **kwargs):
        super(iEEGCoregistrationFrame, self).__init__(**kwargs)
        model = self.model = ElectrodePositionsModel()
        self.surface_visualizer_panel = SurfaceVisualizerPanel(model)
        self.interactive_panel = InteractivePanel(model,
            viz=self.surface_visualizer_panel)

    @on_trait_change('model:_rebuild_vizpanel_event')
    def _rebuild_vizpanel(self):
        self.surface_visualizer_panel = SurfaceVisualizerPanel(self.model)
        self.interactive_panel.viz = self.surface_visualizer_panel

crash_if_freesurfer_is_not_sourced()
iEEGCoregistrationFrame().configure_traits()

