"""This module hosts the `ChargingSim` class, in charge of orchestrating the entire simulation."""

from chargingStation import ChargingStation
import json
import os
import numpy as np
from batterypack import Battery
from batteryAgingSim import BatteryAging
import controller as control  # FILE WITH CONTROL MODULE
import matplotlib.pyplot as plt
from electricityPrices import PriceLoader
from solar import Solar
from node import Node

MINUTES_IN_DAY = 1440


class ChargingSim:
    """
    This is the main orchestrator for the simulation class. It organizes the simulation and controls the propagation of
    other objects' states in a time sequential manner. It is in charge of orchestrating in both RHC/MPC or oneshot
    (offline) modes.

    :param num_charging_sites: Number of charging nodes within the secondary distribution network.
    :param bool solar: If the charging sites have solar PV or not.
    :param int resolution: Time resolution of the simulation.
    :param str path_prefix: Path string that helps ensure simulation can access proper folders within OS file organization.
    :param int num_steps: Number of steps per day. Default is 96 for 15 minute time resolution.
    :param int month: The month for which the simulation is run.
    :param int num_evs: Number of EVs to be simulated.
    :param bool custom_ev_data: If custom EV data is to be used.
    :param str custom_ev_data_path: Path to custom EV data.
    :param bool custom_solar_data: If custom solar data is to be used.
    :param str custom_solar_data_path: Path to custom solar data.
    :param bool centralized: If the DER is centralized or not.
    :param dict central_der_dict: Dictionary of DERs for centralized mode.

    """
    def __init__(self, num_charging_sites, solar=True, resolution=15, path_prefix=None, num_steps=None, month=6,
                 num_evs=1600, custom_ev_data=False, custom_ev_data_path=None, custom_solar_data=False,
                 custom_solar_data_path=None, centralized=False, central_der_dict: dict = None):
        self.centralized = centralized
        self.controller_config = None
        self.num_evs = num_evs
        self.month = month
        self.custom_ev_data = custom_ev_data
        if solar:
            self.solar = True  # to be initialized with module later

        if custom_ev_data:
            charge_data = np.loadtxt(f'{path_prefix}/{custom_ev_data_path}')    # Check this to ensure correct path.
            print('*** Custom charging load data loaded ***')
        else:
            charge_data = np.loadtxt(f'{path_prefix}/data/SPEECh_load_data/speechWeekdayLoad{self.num_evs}.csv')
            print('*** SpeechData loaded ***')
        if centralized:
            if central_der_dict is None:
                raise ValueError("central_der_dict must be specified for centralized mode!")    # TODO: Include more verbose instructions here
            self.central_der_dict = central_der_dict
            self.central_node_dict = {}     # Dictionary of nodes objects for centralized DER, to be populated later.

        self.path_prefix = path_prefix
        self.charge_data = charge_data
        self.solar_config = None
        self.battery_config = None
        self.charging_config = None
        self.prices_config = None
        self.price_loader = None
        self.battery_specs_per_loc = None  # Could be used later to specify varying batteries for various nodes
        self.day_year_count = 0
        self.stop = 0
        self.steps = 0
        self.control_start_index = 0
        self.control_shift = 0
        self.time = 0
        self.test_data = charge_data.flatten()
        self.num_charging_sites = num_charging_sites    # Currently unused.
        self.charging_locs = []
        self.charging_sites = {}
        self.stations_list = []
        self.battery_objects = []
        self.storage_locs = []  # usually empty if not centralized mode
        self.site_net_loads = []
        self.resolution = resolution
        if not num_steps:
            self.num_steps = int(MINUTES_IN_DAY / resolution)
        else:
            self.num_steps = num_steps
        self.aging_sim = None  # This gets updated later
        self._nodes = []
        self.scenario = None    # to be updated later

    def load_config(self):
        """
        Loads all object configuration files and set the config attribute within the class. Walks through
        the os files, finds the config JSON files, and loads them as the attributes.

        :return: None.
        """

        # use master config for loading other configs also change all these paths from literal
        configs_path = f'{self.path_prefix}/charging_sim/configs'
        current_working_dir = os.getcwd()
        os.chdir(configs_path)
        for root, dirs, files, in os.walk(configs_path):
            for file in files:
                attribute = file.split(".")[0] + "_config"
                with open(file, "r") as f:
                    config = json.load(f)
                    setattr(self, attribute, config)
        os.chdir(current_working_dir)  # return back to current working directory
        self.load_battery_params()  # update the battery params to have model dynamics for all cells loaded already

    def load_battery_params(self):
        """
        This loads the battery params directly into the sim, so parameters will be the same for all
        batteries unless otherwise specified. battery_config must be attributed to do this.

        :return: None.
        """
        params_list = [key for key in self.battery_config.keys() if "params_" in key]
        for params_key in params_list:
            self.battery_config[params_key] = np.loadtxt(f'{self.path_prefix}/{self.battery_config[params_key]}') # Replace path with true value.
        # Reverse directionality is important for numpy.interp function.
        self.battery_config["OCV_map_voltage"] = np.loadtxt(f'{self.path_prefix}/{self.battery_config["OCV_map_voltage"]}')[::-1]
        self.battery_config["OCV_map_SOC"] = np.loadtxt(f'{self.path_prefix}/{self.battery_config["OCV_map_SOC"]}')[::-1]

    def create_battery_object(self, idx, node_name, controller=None):
        """
        Creates and stores all battery modules/objects in the network.

        :param idx: Battery identification index.
        :param str node_name: Name of node_name.
        :param controller: Controller assigned to the battery object.

        :return (object): Battery object.
        """
        buffer_battery = Battery(config=self.battery_config, controller=controller)
        buffer_battery.id, buffer_battery.node = idx, node_name
        buffer_battery.num_cells = buffer_battery.battery_setup()  # Toggle between setup and setuo_2 to scale kWh
        # energy capacity using voltage changed this to try scaling voltage instead.
        buffer_battery.load_pack_props()    # This is for simulating the entire pack at once.
        self.battery_objects += buffer_battery,  # Add to list of battery objects.
        return buffer_battery

    def create_charging_stations(self, power_nodes):
        """
        Creates the charging station objects within the power network (MPC mode).

        :param list power_nodes: List of buses/nodes which can host charging stations. It is a list of dicts.

        :return: None
        """
        loc_list = power_nodes
        for i in range(len(loc_list)):
            node_name = loc_list[i]['node_name']
            battery = self.create_battery_object(i, node_name)  # change this from float param to generic
            solar = self.create_solar_object(i, node_name)  # create solar object
            self.controller_config['opt_solver'] = self.scenario['opt_solver']  # set the optimization solver
            controller = control.MPC(self.controller_config, storage=battery,
                                     solar=solar)  # need to change this to load based on the users controller python file?
            self.charging_config['locator_index'], self.charging_config['location'] = i, node_name
            self.charging_config['L2'] = loc_list[i]['L2']
            self.charging_config['DCFC'] = loc_list[i]['DCFC']
            # todo: check if load data the default speech data is being used or not.
            if not self.custom_ev_data:
                self.charging_config['data_path'] = f'{self.path_prefix}/data/SPEECh_load_data/speechWeekdayLoad{self.num_evs}.csv'  # This can be changed if you have your own data.
            charging_station = ChargingStation(self.charging_config, controller=controller,
                                               storage=battery, solar=solar)
            self.charging_sites[node_name] = charging_station
        self.stations_list = list(self.charging_sites.values())
        self.charging_locs = list(self.charging_sites.keys())

    def create_charging_stations_centralized(self, charging_nodes_list):
        """
        Creates charging stations for centralized DER shared within a distribution node_name. The centralized option is used
        to study the value of centralization of DER within primary feeder nodes. For example if an EV Service Provider
        owns a fleet of charging stations within a primary feeder node_name, then the DER can be centralized and shared
        within that node to offset its power consumption.

        :param list(dict) charging_nodes_list: List of charging node dicts necessary to configure simulation,
         especially the node name and location to map charging station objects to the power network.

        :return: None.
        """
        # todo: include inverter params and find rough costs for that.
        node_index = 0
        for node_name in self.central_der_dict.keys():
            battery = self.create_battery_object(node_index, node_name)  # Centralized battery object.
            solar = self.create_solar_object(node_index, node_name)  # Create centralized solar object.
            self.controller_config['opt_solver'] = self.scenario['opt_solver']  # set the optimization solver
            controller = control.MPC(self.controller_config, storage=battery,
                                     solar=solar)  # need to change this to load based on the users controller python file?
            bus_node_object = Node(node_name, node_index, storage=battery, solar=solar, controller=controller)
            self.central_node_dict[node_name] = bus_node_object  # Populate the dictionary of node_name objects with the node_name object.
            node_index += 1

        for i in range(len(charging_nodes_list)):
            self.charging_config['locator_index'], self.charging_config['location'] = i, charging_nodes_list[i]['node_name']
            self.charging_config['L2'] = charging_nodes_list[i]['L2']
            self.charging_config['DCFC'] = charging_nodes_list[i]['DCFC']
            if not self.custom_ev_data:
                self.charging_config[
                    'data_path'] = f'{self.path_prefix}/data/SPEECh_load_data/speechWeekdayLoad{self.num_evs}.csv'
            charging_station = ChargingStation(self.charging_config)
            self.charging_sites[charging_nodes_list[i]['node_name']] = charging_station  # Dictionary of charging stations.
        self.stations_list = list(self.charging_sites.values())     # Might remove this later if not needed.
        self.charging_locs = list(self.charging_sites.keys())   # Might remove this later if not needed.

    def create_charging_stations_oneshot(self, power_nodes):
        """
        Creates the charging station objects within the power network (offline mode).

        :param list power_nodes: List of buses/nodes which can host charging stations.
        :return: None.
        """
        loc_list = power_nodes
        # make a list of dicts with varying capacities
        # todo: change the starting point based on existing charging stations
        for i in range(len(loc_list)):
            node_name = loc_list[i]['node_name']
            battery = self.create_battery_object(i, node_name)  # change this from float param to generic
            solar = self.create_solar_object(i, node_name)  # create solar object
            self.controller_config['opt_solver'] = self.scenario['opt_solver']  # set the optimization solver
            controller = control.Oneshot(self.controller_config, storage=battery,
                                     solar=solar, num_steps=self.num_steps)  # need to change this to load based on the users controller python file?
            self.charging_config['locator_index'], self.charging_config['location'] = i, node_name
            self.charging_config['L2'] = loc_list[i]['L2']
            self.charging_config['DCFC'] = loc_list[i]['DCFC']
            if not self.custom_ev_data:
                self.charging_config['data_path'] = f'{self.path_prefix}/data/SPEECh_load_data/speechWeekdayLoad{self.num_evs}.csv'
            charging_station = ChargingStation(self.charging_config, controller=controller,
                                               storage=battery, solar=solar)
            self.charging_sites[node_name] = charging_station
        self.stations_list = list(self.charging_sites.values())
        self.charging_locs = list(self.charging_sites.keys())

    def initialize_aging_sim(self):
        """
        Initialize the battery aging object.

        :return: None.
        """
        num_steps = 1
        self.aging_sim = BatteryAging(0, num_steps)

    def initialize_price_loader(self, month):
        """
        Loads the price loading module and sets the month to be simulated for memory-efficient sampling.

        :param int month: Month to be simulated.
        :return: None.
        """
        configs_path = f'{self.path_prefix}/charging_sim/configs'
        current_working_dir = os.getcwd()
        self.price_loader = PriceLoader(self.prices_config, path_prefix=self.path_prefix)
        self.price_loader.set_month_data(month)
        input_data_res = self.prices_config["resolution"]
        if input_data_res > self.resolution:
            self.price_loader.downscale(input_data_res, self.resolution)
            self.prices_config["resolution"] = self.resolution
            file_path_list = self.prices_config["data_path"].split("_")
            new_data_path = self.prices_config["data_path"].replace(file_path_list[-1],
                                                                    str(self.resolution) + "min.csv")
            self.prices_config["data_path"] = new_data_path
            with open(self.prices_config["config_path"], 'w') as config_file_path:
                os.chdir(configs_path)
                json.dump(self.prices_config, config_file_path, indent=1)
            os.chdir(current_working_dir)

    def create_solar_object(self, idx, loc, controller=None):
        """
        Creates a solar object using the solar class ``charging_sim.solar``.

        :param int idx: Solar object identifier.
        :param loc: Location of solar object within the grid.
        :param controller: Controller object of solar system.
        :return object: Solar object.
        """
        solar = Solar(config=self.solar_config, path_prefix=self.path_prefix,
                      controller=controller, num_steps=self.num_steps)  # remove Q_initial later
        solar.id, solar.node = idx, loc  # using one index to represent both id and location
        return solar

    def reset_loads(self):
        """
        Resets the loads at the different buses being tracked.

        :return: None.
        """
        self.site_net_loads = []

    def get_charging_sites(self):
        """
        Returns the charging station locations within the grid.

        :return list locs: Charging station locations.
        """
        return self.charging_locs

    def get_charger_obj_by_loc(self, loc):
        """
        Returns the charging station object at given location 'loc'.

        :param str loc: Location of charging station.
        :return object: Charging station object.
        """
        return self.charging_sites[loc]

    def setup(self, power_nodes_list, scenario=None):
        """
        This is done pre-simulation to ensure all scenarios are updated accordingly.
        This method takes in a list of dicts with the following keys: `node_name`, `L2`, `DCFC`.
        The setup accounts for the different modes of simulation. It also updates the scenario dict to match the
        specifications of the given scenario.

        :param list power_nodes_list: List of buses dictionaries for which EVSE/Charging Station exists.
        :param scenario: Contains specifications for the scenario, such as battery capacity, c-rate, solar, etc.
                        See example `user_input.json` file for full specifications.

        :return: None.
        """
        self.load_config()  # FIRST LOAD THE CONFIG ATTRIBUTES
        self.update_scenario(scenario)  # scenarios for study
        self.scenario = scenario
        if self.scenario:
            if self.scenario['oneshot']:
                # TODO: add centralized DER mode for oneshot.
                print("One shot optimization loading...")
                self.create_charging_stations_oneshot(power_nodes_list)
            else:
                if self.centralized:
                    print("Centralized DER mode loading...")
                    self.create_charging_stations_centralized(power_nodes_list)
                else:
                    print("Collocated DER mode loading...")
                    self.create_charging_stations(power_nodes_list)
            self.initialize_price_loader(self.prices_config["month"])
            self.initialize_aging_sim()  # Battery aging

    def update_scenario(self, scenario=None):
        """
        Updates the scenarios dicts to match specifications of a given scenario.

        :param scenario: The scenario dict to be modified, if given.
        :return: None.
        """
        # Todo: make this cleaner by adding a method to update each of the config codes that look repetitive.
        if scenario:
            self.prices_config['month'] = scenario['start_month']
            self.charging_config['month'] = scenario['start_month']
            if self.solar:
                for key in scenario['solar'].keys():
                    if scenario['solar'][key]:
                        self.solar_config[key] = scenario['solar'][key]
            for key in scenario['battery'].keys():
                if scenario['battery'][key]:
                    self.battery_config[key] = scenario['battery'][key]
            for key in scenario['charging_station'].keys():
                if scenario['charging_station'][key]:
                    self.charging_config[key] = scenario['charging_station'][key]
            for key in scenario['elec_prices'].keys():
                if scenario['elec_prices'][key]:
                    print('Updating electricity price data path...')
                    self.prices_config[key] = scenario['elec_prices'][key]
            if scenario['load']['data_path']:
                print('Updating load data path...')
                self.charge_data = scenario['load']['data_path']

            print('New scenario updated...')

    def update_steps(self, steps):
        """
        Updates for moving simulation forward.

        :param int steps: Number of steps to move forward.
        :return: None.
        """
        self.steps += steps
        if self.steps == MINUTES_IN_DAY / self.resolution:
            self.day_year_count += 1

    @staticmethod
    def get_action(self):
        """Returns only the control current."""
        raise NotImplementedError("Function not implemented yet!")

    def initialize_controllers(self):
        """Assign charging controller to each EVSE. """
        raise NotImplementedError("Function not implemented yet!")

    def step(self, stepsize):
        """
        This assumes perfect load foresight, doing daily propagation for Charging Station sequentially within the power
        grid. This can easily be modified to include forecast errors and uncertainty.

        :param int stepsize: Number of steps to take.
        :return: None.
        """
        self.reset_loads()  # reset the loads from old time-step
        elec_price_vec = self.price_loader.get_prices(self.time, self.num_steps)    # time already accounted for
        for charging_station in self.stations_list:  # TODO: how can this be efficiently parallelized ?
            if self.time % 96 == 0:
                charging_station.controller.reset_load()
                self.time = 0  # reset time
            p = charging_station.controller.solar.get_power(self.time, self.num_steps)  # can set month for multi-month sim later
            buffer_battery = charging_station.storage
            self.control_start_index = stepsize * self.day_year_count
            todays_load = charging_station.load_data[self.day_year_count*self.num_steps+self.time
                                         :self.num_steps*(self.day_year_count+1)+self.time] * 1 # this is where time comes in
            plt.plot(todays_load)
            plt.savefig('test_{}.png'.format(self.day_year_count))
            plt.close()
            todays_load = np.minimum(todays_load, charging_station.capacity)   # only accept capacity of charging station
            todays_load = todays_load.reshape(-1, 1)
            for i in range(stepsize):
                control_action = charging_station.controller.compute_control(todays_load, elec_price_vec)
                charging_station.controller.load += todays_load[i],
                buffer_battery.dynamics(control_action)
                net_load = todays_load[0, 0] + buffer_battery.power - charging_station.solar.power[0, 0]   # moving horizon so always only pick the first one
                charging_station.update_load(net_load, todays_load[0, 0])   # in kW
            # check whether one year has passed (not relevant since we don't run one full year yet)
            if self.day_year_count % 365 == 0:
                self.day_year_count = 0
            buffer_battery.update_capacity()  # update the linear aging vector to include linear aging for previous run
            self.aging_sim.run(buffer_battery)  # simulate battery aging
            charging_station.controller.battery_initial_SOC = charging_station.controller.battery_SOC.value[1, 0]
        self.time += 1
        self.update_steps(stepsize)

    def multistep(self):
        """
        This is used oneshot offline simulation for any given month. It is much faster than the Receding Horizon Control
        (RHC or MPC) mode and it used to propagate the states of all objects through the simulation horizon.

        This assumes perfect load foresight, doing daily propagation for Charging Station sequentially within the power
        grid.

        :return: None.
        """
        elec_price_vec = self.price_loader.get_prices(self.time, self.num_steps)  # time already accounted for
        for charging_station in self.stations_list:  # TODO: how can this be efficiently parallelized ?
            p = charging_station.controller.solar.get_power(self.time, self.num_steps, desired_shape=(self.num_steps, 1))  # can set month for multi-month sim later
            buffer_battery = charging_station.storage
            todays_load = charging_station.load_data[self.day_year_count * self.num_steps + self.time
                                         :self.num_steps * (self.day_year_count + 1) + self.time] * 1  # this is where time comes in
            todays_load = np.minimum(todays_load, charging_station.capacity).reshape(-1, 1)
            control_actions = charging_station.controller.compute_control(todays_load, elec_price_vec)
            charging_station.controller.load = todays_load
            battery_powers = []
            for interval in range(self.num_steps):
                charging_station.storage.dynamics(control_actions[interval, 0])
                self.aging_sim.run(buffer_battery)
                battery_powers.append(buffer_battery.power)
            net_load = todays_load + np.array(buffer_battery.power).reshape(-1, 1) - charging_station.solar.power
            charging_station.update_load_oneshot(net_load, todays_load)

    def step_centralized(self, stepsize):
        """
        If mode is centralized DER (multiple charging stations connected to the same primary distribution feeder), then
        this method is used to propagate the states of all objects through the simulation horizon. The price vector is
        the same for all charging stations, but the load vector is different for each charging station. Making the price
        vector the same is reasonable because it is more likely that the primary feeder is the same for all charging
        stations and would exist in the same utility tariff zone or territory.

        :param int stepsize: Number of steps to take.
        :return:
        """
        self.reset_loads()  # reset the loads from old time-step
        elec_price_vec = self.price_loader.get_prices(self.time, self.num_steps)  # time already accounted for
        # First go through all the charging stations per load and then collate them.
        # So each central DER has a controller, battery, and solar object.
        for node_name in self.central_der_dict.keys():
            charging_stations_locs = self.central_der_dict[node_name]    # Map from node_name to charging stations
            central_der_node = self.central_node_dict[node_name]     # Get the node_name object
            controller = central_der_node.controller    # They all have the same controller
            if self.time % 96 == 0:
                controller.reset_load()
                self.time = 0  # reset time
            buffer_battery = controller.storage
            solar = controller.solar
            todays_load = np.zeros((self.num_steps,))
            p = solar.get_power(self.time, self.num_steps, desired_shape=(self.num_steps, 1))

            for loc in charging_stations_locs:
                # Sum all the expected EV charging loads from all the charging stations.
                # Sum up the aggregate load.
                charging_station = self.get_charger_obj_by_loc(loc)
                todays_load += np.minimum(charging_station.capacity, charging_station.load_data[self.day_year_count *
                                self.num_steps + self.time:self.num_steps * (self.day_year_count + 1) + self.time] * 1) # this is where time comes in
            self.control_start_index = stepsize * self.day_year_count
            todays_load = todays_load.reshape(-1, 1)
            for i in range(stepsize):
                control_action = controller.compute_control(todays_load, elec_price_vec)
                controller.load += todays_load[i],
                buffer_battery.dynamics(control_action)
                net_load = buffer_battery.power - solar.power[0, 0]  # moving horizon so always only pick the first one
                central_der_node.update_load(net_load, todays_load[0, 0])  # in kW
            # check whether one year has passed (not relevant since we don't run one full year yet)
            if self.day_year_count % 365 == 0:
                self.day_year_count = 0
            buffer_battery.update_capacity()  # update the linear aging vector to include linear aging for previous run
            self.aging_sim.run(buffer_battery)  # simulate battery aging
            controller.battery_initial_SOC = controller.battery_SOC.value[1, 0]
        self.time += 1
        self.update_steps(stepsize)

    def load_results_summary(self, save_path_prefix, plot=False):
        """
        Saves the simulation results to a specified path. It also plots some results if specified. The results are
        saved in a folder with the name of the simulation scenario. The results are saved in a .csv file. The results
        include the following: load, solar, battery power, voltages, rate Block, battery SOC, battery calendar aging,
        battery cycle aging, battery SOH, cycles etc.

        :param save_path_prefix: Includes prefix to desired path for saving results.
        :param boolean plot: Decides if some results are plotted or not.
        :return: None.
        """
        if self.centralized:
            for power_node in self.central_der_dict.keys():
                node = self.central_node_dict[power_node]
                node.save_sim_data(save_path_prefix)
            for battery in self.battery_objects:
                battery.save_sim_data(save_path_prefix)
        else:
            for charging_station in self.stations_list:
                charging_station.save_sim_data(save_path_prefix)
            for battery in self.battery_objects:
                battery.save_sim_data(save_path_prefix)

        # SOME ADDITIONAL PLOTS BELOW
        if plot:
            fig, ax1 = plt.subplots()
            ax2 = ax1.twinx()
            ax1.plot(battery.SOC_track[1:], label='battery SoC')
            ax2.plot(battery.calendar_aging[1:], color='r', ls='--', label='calendar aging')
            ax1.legend()
            ax2.legend()
            ax1.set_xlabel('Time step')
            ax1.set_ylabel('SOC')
            ax2.set_ylabel('Calendar aging')
            plt.savefig(save_path_prefix + "/Calendar_SOC_plot.png")
            plt.close('all')

            fig, ax1 = plt.subplots()
            ax2 = ax1.twinx()
            ax1.plot(battery.SOC_track[1:], label='battery SOC')
            ax2.plot(battery.cycle_aging[1:], color='r', ls='--', label='cycle aging')
            ax1.set_xlabel('Time step')
            ax1.set_ylabel('SOC')
            ax2.set_ylabel('Cycle aging')
            ax1.legend()
            ax2.legend()
            plt.savefig(save_path_prefix + "/Cycle_SOC_plot.png")
            plt.close('all')

        print("total calendar aging is {}".format(sum(battery.calendar_aging)))
        print("total cycle aging is {}".format(sum(battery.cycle_aging)))
        print("Final capacity (SOH) is {}".format(battery.SOH))
        print("Total current throughput is {}".format(battery.total_amp_thruput))