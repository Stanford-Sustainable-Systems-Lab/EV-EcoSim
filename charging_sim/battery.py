import json
import numpy as np
from utils import num_steps
import matplotlib.pyplot as plt

# Model assumes symmetry from charging to discharging dynamics. Asymmetry is negligible for most purposes.

# TODO: add battery simulation resolution control so can be different from control as well...
class Battery:
    """
     Properties:
         max voltage(V)
         min Voltage (V)
         nominal energy (kWh)
         id (-)
         ambient temperature (C)

     Assumptions:
         efficiency already incorporates resistance; improve later.
         assumes linear aging model for SOH, SOC, and voltage variation, but relaxed by parameter update.
         Temperature variation in battery environment is negligible.

    """

    def __init__(self, battery_type, Q_initial, node=None, config=None):
        """should I make in terms of rc pairs or how should I call dynamics..data-driven vs ECM"""
        self._node = node  # This is used for battery location.
        #   TODO: need to include resolution and others
        self.resolution = config["resolution"]
        self.dt = config["resolution"] / 60     # in hours
        self.cap = config["cell_nominal_cap"]  # Ah
        self.nominal_cap = config["cell_nominal_cap"]
        self.cell_resistance = config["resistance"]  # TO be updated
        self._eff = config["round-trip_efficiency"]
        self.max_c_rate = config["max_c_rate"]
        # self.nominal_energy = config["cell_nominal_energy"]     # Watts-hours

        # LOAD THE PARAMETERS FOR CERTAIN CYCLE INTERVAL
        self.params_0_cycles = config["params_0_cycles"]
        self.params_25_cycles = config["params_25_cycles"]
        self.params_75_cycles = config["params_75_cycles"]
        self.params_125_cycles = config["params_125_cycles"]
        # TODO: think about how to load these parameter changes over time
        self.OCV_map_SOC = config["OCV_map_SOC"]   # update these two as well
        self.OCV_map_voltage = config["OCV_map_voltage"]   # update these two as well

        self.max_current = self.max_c_rate * self.cap   # figure out a way to have these update automatically
        self.A_Ro = self.params_0_cycles[0]
        self.B_Ro = self.params_0_cycles[1]
        self.C_Ro = self.params_0_cycles[2]
        self.R1 = self.params_0_cycles[3]
        self.C1 = self.params_0_cycles[4]
        self.R2 = self.params_0_cycles[5]
        self.C2 = self.params_0_cycles[6]

        self._name = battery_type
        self.iR1 = 0
        self.iR2 = 0

        self.voltage = config["max_cell_voltage"]
        self.max_voltage = config["max_cell_voltage"]
        self.min_voltage = config["min_cell_voltage"]  # for each cell
        self.nominal_voltage = config["nominal_cell_voltage"]  # for each cell
        self.nominal_energy = self.nominal_voltage * self.nominal_cap
        self.Qmax = config["SOC_max"] * self.cap
        self.Qmin = config["SOC_min"] * self.cap
        self.initial_SOC = config["SOC"]
        self.min_SOC = config["SOC_min"]
        self.max_SOC = config["SOC_max"]
        self.OCV = config["max_cell_voltage"]
        self.SOH = config["SOH"]
        self.daily_self_discharge = config["daily_self_discharge"]

        # battery_setup updates with tuple (no_cells_series, no_modules_parallel, total_cells)
        self.SOC_track = [self.initial_SOC]
        self.SOH_track = [self.SOH]     # to be finished later
        self.calendar_aging = [0.0]    # tracking calendar aging
        self.cycle_aging = [0.0]   # tracking cycle aging
        self.control_power = np.array([])

        self.voltages = [self.voltage]  # to be updated at each time-step (seems expensive)
        self.current_voltage = 0.0
        self.true_voltage = np.array([])  # discharge/charge voltage per time-step
        # self.Q = cp.Variable((num_steps + 1, 1))  # Amount of energy Kwh available in battery
        self.SOC = self.initial_SOC
        self.SOC_list = [self.initial_SOC]
        self.Ro = self.B_Ro * np.exp(self.SOC) + self.A_Ro * np.exp(self.C_Ro * self.SOC)   # optional

        self.Q_initial = Q_initial  # include the units here
        self.control_current = np.array([])
        self.total_amp_thruput = 0.0
        self.currents = [0]

        # self.power_charge = cp.Variable((num_steps, 1))
        # self.power_discharge = cp.Variable((num_steps, 1))
        self.power = 0
        self.current = 0
        self.true_power = []
        self.start = config["start_time"]
        self.MPC_Control = {'Q': [], 'P': []}  # tracks the control actions for the MPC control
        self.size = 100  # what does this mean?
        self.cell_count = 0

        self.total_aging = 0
        self.true_capacity_loss = 0
        self.resistance_growth = 0
        self.true_aging = []  # want to keep track to observe trends based on degradation models
        self.linear_aging = []  # linear model per Hesse Et. Al

        self.ambient_temp = 25  # Celsius (NOT USED YET - FUTURE VERSION COULD USE THIS)
        self.charging_costs = 0
        self.power_profile = {'Jan': [], 'Feb': [], 'Mar': [], 'Apr': [], 'May': [], 'Jun': [], 'Jul': [], 'Aug': [],
                              'Sep': [], 'Oct': [], 'Nov': [], 'Dec': []}
        # self.Ro = self.get_Ro()

        self.topology = [1, 1]  # DEFAULT VALUE: singular cell
        self.constraints = None
        self.id = None  # Use Charging Station ID for this
        self.savings = None
        self.state_of_charge = None
        self.nominal_pack_voltage = config["pack_voltage"]    # to be initialized later
        self.nominal_pack_cap = config["pack_energy_cap"]
        self.predicted_voltages = list()
        self.operating_voltages = list()

    def get_true_power(self):
        return np.array(self.true_power)

    def battery_setup(self):
        """
        TODO: I NEED TO CHECK THAT THE PACK MATCHES THE OUTPUT POWER CORRECTLY
        Capacity (Wh)
        Voltage (V)
        params: (cell_amp_hrs (Ah), cell_voltage (V)
        TODO: Finish this up and scale battery up completely.
            Do comparison algo setup with real battery and parameter adjustment."""
        # number of modules in parallel should be determined by the power rating and Voltage
        # should use nominal voltage and max allowable current
        pack_capacity_Ah = self.nominal_pack_cap / self.nominal_pack_voltage
        print("Capacity is: ", pack_capacity_Ah, round(pack_capacity_Ah))
        pack_capacity_Ah = self.nominal_pack_cap // self.nominal_pack_voltage
        cell_amp_hrs = self.cap
        cell_voltage = self.nominal_voltage
        no_cells_series = self.nominal_pack_voltage // cell_voltage
        no_modules_parallel = pack_capacity_Ah // cell_amp_hrs
        self.cell_count = no_cells_series * no_modules_parallel
        self.topology = (no_cells_series, no_modules_parallel, self.cell_count)
        self.nominal_pack_voltage = no_cells_series * self.nominal_voltage
        self.nominal_pack_cap = no_modules_parallel * self.cap
        print("***** Battery initialized. *****\n",
              "Battery pack capacity is {} Ah".format(pack_capacity_Ah),
              "Total number of cells is: {} .\n".format(self.cell_count),
              "no. cells in series is: {} \n. No modules in parallel is: {}".format(no_cells_series, no_modules_parallel))
        return self.cell_count

    def battery_setup_tesla(self, model=3):
        """Using TESLA mode to setup battery config. TO BE IMPLEMENTED LATER"""
        if model == 3:
            pass
        elif model == 's':
            pass

    def est_calendar_aging(self):
        """Estimates the constant calendar aging of the battery. this is solely time-dependent.
        Deprecate this later for this object"""
        life_cal_years = 10
        seconds_in_min = 60
        seconds_in_year = 31556952
        aging_cal = (self.resolution * seconds_in_min) / (life_cal_years * seconds_in_year)
        aging_cal *= np.ones((num_steps, 1))
        return np.sum(aging_cal)

    def est_cyc_aging(self):
        """Creates linear battery ageing model per hesse. et al, and returns its cvx object.
        Deprecate this later for this object"""
        seconds_in_min = 60
        life_cyc = 4500     # change this to be input in the config file
        aging_cyc = (0.5 * (np.sum(abs(self.current * self.voltage))
                            * self.resolution / seconds_in_min)) / (life_cyc / 0.2 * self.nominal_energy)
        return aging_cyc

    def get_power_profile(self, months):
        power_profiles_dict = {}
        for month in months:
            power_profiles_dict[month] = self.power_profile[month]
        return power_profiles_dict

    def get_total_aging(self):
        return self.est_cyc_aging() + self.est_calendar_aging()

    def get_aging_value(self):
        """returns the actual aging value lost after a cvxpy run..."""
        aging_value = self.est_cyc_aging() + self.est_calendar_aging()
        return aging_value

    def update_capacity(self):
        """This is not true capacity but anticipated capacity based on linear model."""
        aging_value = self.get_aging_value() * 0.2  # do not multiply by 0.2 to keep comparable
        self.total_aging += aging_value
        self.linear_aging.append(aging_value)  # this is to ADD aging for each time step

    def get_Ro(self):
        """This can be updated later to include current...
        Deprecate this possibly"""
        pass
        # Ro = self.ECM_params[1] * cp.exp(self.SOC) + self.ECM_params[0] * cp.exp(self.C_Ro * self.SOC) \
        #      + self.resistance_growth
        # return Ro

    def update_SOC(self):
        action_length = 1   # maybe change this later for variable action length
        self.Q_initial = self.Q.value[action_length]

    def track_SOC(self, SOC):
        self.SOC_track.append(SOC)
        self.SOC_list.append(SOC)

    def update_max_current(self, verbose=False):
        self.max_current = self.max_c_rate * self.cap
        if verbose:
            print("Maximum allowable current updated.")

    def update_voltage(self, voltage):
        self.current_voltage = voltage  # I should be updating initial voltage with new voltage measurement
        self.predicted_voltages.append(voltage)
        # print("Current voltage estimate is: ", voltage)

    def get_properties(self):
        return self.properties

    def visualize(self, option):
        """method is used to plot and save battery states desired by user"""
        if type(option) == str:
            plt.style.use('seaborn-darkgrid')
            if option == "SOC_track":
                plotting_values = getattr(self, option)
                plt.figure()
                plt.plot(plotting_values)
                plt.xlabel("Time Step")
                plt.ylabel("SOC")
                plt.savefig(option + "_{}.png".format(self.id))
                plt.close()
            else:
                plotting_values = getattr(self, option)
                plt.figure()
                plt.plot(plotting_values)
                plt.xlabel("Time Step")
                plt.ylabel(option)
                plt.savefig(option + "_{}.png".format(self.id))
                plt.close()
        print("Est. tot. no. of cycles is: ", self.total_amp_thruput/((self.nominal_pack_cap+self.cap)/2), 'cycles')

    def visualize_voltages(self):
        pass

    def learn_params(self):
        pass

    def update_params(self):
        """This changes the battery params depending on the number of cycles"""
        pass
    
    def dynamics(self, current):
        # currently, dynamics assumes cells are perfectly balanced- Can we account for imbalanced cells later?
        # TODO: FUTURE deal with simulation dual-resolution dynamical system...maybe get finer battery dynamics in here

        dt = self.dt * 3600  # convert from hour to seconds for dynamics equations but not SOC
        # state equations
        self.SOC = self.SOC + current * self.dt / self.cap   # current signs (+) charge (-) discharge
        # For a particular step, if absolute val c-rate is so high that SOC exceeds limits, derate current
        if self.SOC > self.max_SOC:
            print('max SOC violation, readjusting SoC...')
            assert current >= 0     # FOR DEV PURPOSES. REMOVE LATER
            excess_soc = self.SOC - self.max_SOC
            allowable_curr_thruput = round(current * self.dt - excess_soc * self.cap, 4)    # Ah
            # print(current, excess_soc, allowable_curr_thruput, self.cap)
            assert allowable_curr_thruput >= 0  # FOR DEV PURPOSES. REMOVE LATER
            self.current = allowable_curr_thruput / self.dt
            self.total_amp_thruput += abs(self.current) * self.dt   # cycle counting
            # print('SOC violation, readjusting SoC...')
            self.SOC = self.max_SOC
            self.track_SOC(self.SOC)
            self.state_eqn(self.current)
            self.voltages = np.append(self.voltages, self.voltage)
            return self.voltage
        if self.SOC < self.min_SOC:
            print('min SOC violation, readjusting SoC...')
            # current should always be negative here
            deficient_soc = self.min_SOC - self.SOC
            allowable_curr_thruput = abs(current * self.dt + deficient_soc * self.cap)
            assert allowable_curr_thruput >= 0  # for dev purposes
            self.SOC = self.min_SOC
            self.track_SOC(self.SOC)
            self.power = (self.voltage * self.topology[0]) * (self.current * self.topology[1]) / 1000  # kw
            self.current = -allowable_curr_thruput / self.dt  # readjusting current
            self.state_eqn(self.current)     # update states
            self.voltages = np.append(self.voltages, self.voltage)
            self.total_amp_thruput += abs(self.current) * self.dt  # cycle counting
            # self.currents.append(current)
            return self.voltage

        #  state equations
        self.state_eqn(current)     # this updates the battery states
        # self.currents.append(current)

        if self.voltage > self.max_voltage:
            print("charge current too high! Max voltage exceeded")
            # we de-rate the current if voltage is too high (exceeds max prescribed v)
            # voltage can exceed desirable range if c-rate is too high, even when SoC isn't at max
            current -= (self.voltage - self.max_voltage)/self.Ro
            self.voltage = self.max_voltage
            self.state_eqn(current, append=False)
            self.currents[-1] = current
            # raise Exception("Max voltage exceeded even after max SOC flag!!!") this can happen
            self.voltages = np.append(self.voltages, self.voltage)  # numpy array
            self.track_SOC(self.SOC)
            self.total_amp_thruput += abs(current) * self.dt  # cycle counting
            return self.voltage
        elif self.voltage < self.min_voltage:
            print("discharge current too high ! Min voltage exceeded")
            current += (self.min_voltage - self.voltage) / self.Ro
            self.state_eqn(current, append=False)
            self.currents[-1] = current
            self.voltage = self.min_voltage
            self.voltages = np.append(self.voltages, self.voltage)  # numpy array
            self.track_SOC(self.SOC)
            self.total_amp_thruput += abs(current) * self.dt  # cycle counting
            return self.voltage

        self.current = current
        self.power = (self.voltage * self.topology[0]) * (self.current * self.topology[1]) / 1000  # kw
        # print("power is {} kW".format(self.power))
        # self.topology[0] = # of cells in series, self.topology[1] = # in parallel
        self.voltages = np.append(self.voltages, self.voltage)  # numpy array
        self.track_SOC(self.SOC)
        self.total_amp_thruput += abs(current) * self.dt  # cycle counting
        return self.voltage

    def state_eqn(self, current, append=True):
        """This holds the discretized state equations containing the battery dynamics at the cell-level."""
        dt = self.dt * 3600  # convert from hour to seconds for dynamics equations but not SOC
        self.OCV = np.interp(self.SOC, self.OCV_map_SOC, self.OCV_map_voltage)
        self.Ro = self.B_Ro * np.exp(self.SOC) + self.A_Ro * np.exp(self.C_Ro * self.SOC)
        #   state equations
        self.iR1 = np.exp(-dt / (self.R1 * self.C1)) * self.iR1 + (1 - np.exp(-dt / (self.R1 * self.C1))) * current
        self.iR2 = np.exp(-dt / (self.R2 * self.C2)) * self.iR2 + (1 - np.exp(-dt / (self.R2 * self.C2))) * current
        self.voltage = self.OCV + current * self.Ro + self.iR1 * self.R1 + self.iR2 * self.R2
        if append:
            self.currents.append(current)

#   TEST THE BATTERY CODE HERE (code below is to sanity-check the battery dynamics)
def test():
    # TODO: include error checking assertion points later
    battery_config_path = "/home/ec2-user/EV50_cosimulation/charging_sim/configs/battery.json"
    with open(battery_config_path, "r") as f:
        battery_config = json.load(f)
    params_list = [key for key in battery_config.keys() if "params_" in key]
    for params_key in params_list:
        print("testing load battery params: ", params_key)
        battery_config[params_key] = np.loadtxt(battery_config[params_key])
    # do the OCV maps as well
    battery_config["OCV_map_voltage"] = np.loadtxt(battery_config["OCV_map_voltage"])[::-1]     # ascending order
    battery_config["OCV_map_SOC"] = np.loadtxt(battery_config["OCV_map_SOC"])[::-1]     # ascending order

    Q_initial = 3.5
    buffer_battery = Battery("Tesla Model 3", Q_initial, config=battery_config)
    buffer_battery.id, buffer_battery.node = 0, 0

    # test dynamics here
    c = 2.5  # discharging first
    voltages = []
    currents = []
    for i in range(50):
        v = buffer_battery.dynamics(c)
        currents.append(c)
    c = 0
    for i in range(50):
        v = buffer_battery.dynamics(c)
        currents.append(c)
    c = -2.8
    for i in range(100):
        v = buffer_battery.dynamics(c)
        voltages.append(v)
        currents.append(c)
    c = -4
    for i in range(200):
        v = buffer_battery.dynamics(c)
        currents.append(c)
    c = 3
    for i in range(40):
        v = buffer_battery.dynamics(c)
        currents.append(c)
    c = 2.03  # charging (Amperes)
    for i in range(100):
        v = buffer_battery.dynamics(c)
        currents.append(c)

    fig, ax1 = plt.subplots()
    ax2 = ax1.twinx()
    ax1.plot(buffer_battery.voltages, label='voltage')
    ax2.plot(currents, color='k', label='current')
    ax2.plot(buffer_battery.currents, color='r', ls='--', label='adjusted current')
    ax1.set_xlabel('Time step')
    ax1.set_ylabel('Voltage (V)')
    ax2.set_ylabel('Current (A)')
    plt.legend()
    plt.savefig("battery_test_plot")
    plt.close()
    plt.plot(buffer_battery.SOC_list)
    plt.savefig("SOC_battery_test")
    plt.close()
    plt.plot(buffer_battery.currents[1:50])
    print(len(buffer_battery.currents), len(buffer_battery.voltages))
    plt.savefig("currents_battery_test")


if __name__ == "__main__":
    test()
