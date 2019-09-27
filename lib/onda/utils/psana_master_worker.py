#    This file is part of OnDA.
#
#    OnDA is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    OnDA is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with OnDA.  If not, see <http://www.gnu.org/licenses/>.


import sys
import psana
import mpi4py.MPI
#import mpi4py
import math
import time
import datetime


import lib.onda.cfelpyutils.cfelpsana as cpsana

from lib.onda.utils import (
    global_params as gp,
    dynamic_import as dyn_imp
)

de_layer = dyn_imp.import_layer_module('data_extraction_layer',
                                       gp.monitor_params)
extract = getattr(de_layer, 'extract')


class MasterWorker(object):

    NOMORE = 998
    DIETAG = 999
    DEADTAG = 1000

    def __init__(self, map_func, reduce_func, save_func, source, monitor_params):

        debug = False

        self.psana_source = None
        self._buffer = None
        self.event_timestamp = None

        self.mpi_rank = mpi4py.MPI.COMM_WORLD.Get_rank()
        self.mpi_size = mpi4py.MPI.COMM_WORLD.Get_size()
        if self.mpi_rank == 0:
            self.role = 'master'
        else:
            self.role = 'worker'

        self.monitor_params = monitor_params
        psana_params = monitor_params['PsanaParallelizationLayer']

        self.psana_calib_dir = psana_params['psana_calib_dir']

        self.event_rejection_threshold = 10000000000
        self.offline = False
        self.source = source

        # Set offline mode depending on source
        if 'shmem' not in self.source and debug is False:
            self.offline = True
            if not self.source[-4:] == ':idx':
                self.source += ':idx'

        # Set event_rejection threshold
        if psana_params['event_rejection_threshold'] is not None:
            self.event_rejection_threshold = float(psana_params['event_rejection_threshold'])

        # Set map,reduce and extract functions
        self.map = map_func
        self.reduce = reduce_func
        self.save_func = save_func
        self.extract_data = extract

        # The following is executed only on the master node
        if self.role == 'master':

            self.num_reduced_events = 0
            self.num_nomore = 0

            if self.offline is True:
                self.source_runs_dirname = cpsana.dirname_from_source_runs(source)

        return

    def shutdown(self, msg='Reason not provided.'):

        print ('Shutting down: {0}'.format(msg))

        if self.role == 'worker':
            self._buffer = mpi4py.MPI.COMM_WORLD.send(dest=0, tag=self.DEADTAG)
            mpi4py.MPI.Finalize()
            sys.exit(0)

        if self.role == 'master':
         
            try:
                for nod_num in range(1, self.mpi_size()):
                    mpi4py.MPI.COMM_WORLD.isend(0, dest=nod_num,
                                                tag=self.DIETAG)
                num_shutdown_confirm = 0
                while True:
                    if mpi4py.MPI.COMM_WORLD.Iprobe(source=mpi4py.MPI.ANY_SOURCE, tag=0):
                        self._buffer = mpi4py.MPI.COMM_WORLD.recv(source=mpi4py.MPI.ANY_SOURCE, tag=0)
                    if mpi4py.MPI.COMM_WORLD.Iprobe(source=mpi4py.MPI.ANY_SOURCE, tag=self.DEADTAG):
                        num_shutdown_confirm += 1
                    if num_shutdown_confirm == self.mpi_size() - 1:
                        break
                mpi4py.MPI.Finalize()
            except Exception:
                mpi4py.MPI.COMM_WORLD.Abort(0)
            sys.exit(0)
        return

    def start(self, verbose=False):

        if self.role == 'worker':

            req = None
         
            psana.setOption('psana.calib-dir', self.psana_calib_dir)
     
            self.psana_source = psana.DataSource(self.source)
            print self.psana_source
       
            if self.offline is False:
                psana_events = self.psana_source.events()
            else:
                def psana_events_generator():
                    for r in self.psana_source.runs():
                        times = r.times()
                        mylength = int(math.ceil(len(times) / float(self.mpi_size-1)))
                        mytimes = times[(self.mpi_rank-1) * mylength: self.mpi_rank * mylength]
                        for mt in mytimes:
                            yield r.event(mt)
                psana_events = psana_events_generator()
          
            event = {'monitor_params': self.monitor_params}



            # Loop over events and process
            for evt in psana_events:

                if evt is None:
                    continue
             
                # Reject events above the rejection threshold
                event_id = evt.get(psana.EventId)
                timestring = str(event_id).split('time=')[1].split(',')[0]
                timestamp = time.strptime(timestring[:-6], '%Y-%m-%d %H:%M:%S.%f')
                timestamp = datetime.datetime.fromtimestamp(time.mktime(timestamp))
                timenow = datetime.datetime.now()

                if (timenow - timestamp).total_seconds() > self.event_rejection_threshold:
                    continue
                
                self.event_timestamp = timestamp
                self.epoch_seconds, self.epoch_nanoseconds = event_id.time()
                self.fiducial = event_id.fiducials()
                
                # Check if a shutdown message is coming from the server
                if mpi4py.MPI.COMM_WORLD.Iprobe(source=0, tag=self.DIETAG):
                    self.shutdown('Shutting down RANK: {0}.'.format(self.mpi_rank))
                
                event['evt'] = evt
         
                
                self.extract_data(event, self)

                if self.acqiris_data_wf is None or self.eImage is None or self.pulse_eng is None or self.ebeam_eng is None:

                    continue

                result = self.map()

                # send the mapped event data to the master process
                if req:
                    req.Wait()  # be sure we're not still sending something
                req = mpi4py.MPI.COMM_WORLD.isend(result, dest=0, tag=0)

            # When all events have been processed, send the master a
            # dictionary with an 'end' flag and die
            end_dict = {'end': True}
            if req:
                req.Wait()  # be sure we're not still sending something
            mpi4py.MPI.COMM_WORLD.isend((end_dict, self.mpi_rank), dest=0, tag=0)
            mpi4py.MPI.Finalize()
            sys.exit(0)

        # The following is executed on the master
        elif self.role == 'master':

            if verbose:
                print ('Starting master.')

            # Loops continuously waiting for processed data from workers
            while True:

                try:

                    buffer_data = mpi4py.MPI.COMM_WORLD.recv(
                        source=mpi4py.MPI.ANY_SOURCE,
                        tag=0)
                        
                    if 'end' in buffer_data[0].keys():
                        print ('Finalizing {0}'.format(buffer_data[1]))
                        self.num_nomore += 1
                        if self.num_nomore == self.mpi_size - 1:

                            print('All workers have run out of events.')
                            
                            self.save_func()                                                    
                            print('Shutting down.')
                            self.end_processing()

                            mpi4py.MPI.Finalize()
                            sys.exit(0)
                        continue

                    self.reduce(buffer_data)
                    self.num_reduced_events += 1

                except KeyboardInterrupt as e:
                    print ('Recieved keyboard sigterm...')
                    print (str(e))
                    print ('shutting down MPI.')
                    self.shutdown()
                    print ('---> execution finished.')
                    sys.exit(0)

        return

    def end_processing(self):
        print('Processing finished. Processed {0} events in total.'.format(self.num_reduced_events))

        pass
