# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
import io
import os
from datetime import datetime, timedelta
from time import time
from typing import List, Optional, Dict, cast, Union, Set

from PyQt5.QtCore import QObject, pyqtSignal, QUrl, pyqtProperty, pyqtSlot

from UM import i18nCatalog
from UM.FileHandler.FileWriter import FileWriter
from UM.FileHandler.FileHandler import FileHandler
from UM.Logger import Logger
from UM.Message import Message
from UM.OutputDevice import OutputDeviceError
from UM.Scene.SceneNode import SceneNode
from UM.Version import Version
from cura.CuraApplication import CuraApplication
from cura.PrinterOutput.PrinterOutputController import PrinterOutputController
from cura.PrinterOutput.MaterialOutputModel import MaterialOutputModel
from cura.PrinterOutput.NetworkedPrinterOutputDevice import AuthState
from cura.PrinterOutput.PrinterOutputModel import PrinterOutputModel
from plugins.UM3NetworkPrinting.src.BaseCuraConnectDevice import BaseCuraConnectDevice
from plugins.UM3NetworkPrinting.src.Cloud.CloudApiClient import CloudApiClient
from plugins.UM3NetworkPrinting.src.UM3PrintJobOutputModel import UM3PrintJobOutputModel
from .Models import (
    CloudClusterPrinter, CloudClusterPrintJob, CloudJobUploadRequest, CloudJobResponse, CloudClusterStatus,
    CloudClusterPrinterConfigurationMaterial, CloudErrorObject,
    CloudPrintResponse
)


## Class that contains all the translations for this module.
class T:
    # The translation catalog for this device.

    _I18N_CATALOG = i18nCatalog("cura")

    PRINT_VIA_CLOUD_BUTTON = _I18N_CATALOG.i18nc("@action:button", "Print via Cloud")
    PRINT_VIA_CLOUD_TOOLTIP = _I18N_CATALOG.i18nc("@properties:tooltip", "Print via Cloud")

    CONNECTED_VIA_CLOUD = _I18N_CATALOG.i18nc("@info:status", "Connected via Cloud")
    BLOCKED_UPLOADING = _I18N_CATALOG.i18nc("@info:status", "Sending new jobs (temporarily) blocked, still sending "
                                                            "the previous print job.")

    COULD_NOT_EXPORT = _I18N_CATALOG.i18nc("@info:status", "Could not export print job.")
    WRITE_FAILED = _I18N_CATALOG.i18nc("@info:status", "There are no file formats available to write with!")

    SENDING_DATA_TEXT = _I18N_CATALOG.i18nc("@info:status", "Sending data to remote cluster")
    SENDING_DATA_TITLE = _I18N_CATALOG.i18nc("@info:status", "Sending data to remote cluster")

    ERROR = _I18N_CATALOG.i18nc("@info:title", "Error")
    UPLOAD_ERROR = _I18N_CATALOG.i18nc("@info:text", "Could not upload the data to the printer.")

    UPLOAD_SUCCESS_TITLE = _I18N_CATALOG.i18nc("@info:title", "Data Sent")
    UPLOAD_SUCCESS_TEXT = _I18N_CATALOG.i18nc("@info:status", "Print job was successfully sent to the printer.")


##  The cloud output device is a network output device that works remotely but has limited functionality.
#   Currently it only supports viewing the printer and print job status and adding a new job to the queue.
#   As such, those methods have been implemented here.
#   Note that this device represents a single remote cluster, not a list of multiple clusters.
#
#   TODO: figure our how the QML interface for the cluster networking should operate with this limited functionality.
class CloudOutputDevice(BaseCuraConnectDevice):

    # The interval with which the remote clusters are checked
    CHECK_CLUSTER_INTERVAL = 2.0  # seconds

    # Signal triggered when the printers in the remote cluster were changed.
    printersChanged = pyqtSignal()

    # Signal triggered when the print jobs in the queue were changed.
    printJobsChanged = pyqtSignal()

    ## Creates a new cloud output device
    #  \param api_client: The client that will run the API calls
    #  \param device_id: The ID of the device (i.e. the cluster_id for the cloud API)
    #  \param parent: The optional parent of this output device.
    def __init__(self, api_client: CloudApiClient, device_id: str, parent: QObject = None) -> None:
        super().__init__(device_id = device_id, address = "", properties = {}, parent = parent)
        self._api = api_client

        self._setInterfaceElements()
        
        self._device_id = device_id
        self._account = CuraApplication.getInstance().getCuraAPI().account

        # We use the Cura Connect monitor tab to get most functionality right away.
        self._monitor_view_qml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "../../resources/qml/ClusterMonitorItem.qml")
        self._control_view_qml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "../../resources/qml/ClusterControlItem.qml")
        
        # Properties to populate later on with received cloud data.
        self._print_jobs = []  # type: List[UM3PrintJobOutputModel]
        self._number_of_extruders = 2  # All networked printers are dual-extrusion Ultimaker machines.
        
        # We only allow a single upload at a time.
        self._sending_job = False
        self._progress_message = None  # type: Optional[Message]

    ##  Set all the interface elements and texts for this output device.
    def _setInterfaceElements(self):
        self.setPriority(2)  # make sure we end up below the local networking and above 'save to file'
        self.setName(self._id)
        self.setShortDescription(T.PRINT_VIA_CLOUD_BUTTON)
        self.setDescription(T.PRINT_VIA_CLOUD_TOOLTIP)
        self.setConnectionText(T.CONNECTED_VIA_CLOUD)
    
    ##  Called when Cura requests an output device to receive a (G-code) file.
    def requestWrite(self, nodes: List[SceneNode], file_name: Optional[str] = None, limit_mime_types: bool = False,
                     file_handler: Optional[FileHandler] = None, **kwargs: str) -> None:
        
        # Show an error message if we're already sending a job.
        if self._sending_job:
            self._onUploadError(T.BLOCKED_UPLOADING)
            return
        
        # Indicate we have started sending a job.
        self._sending_job = True
        self.writeStarted.emit(self)

        file_format = self._determineFileFormat(file_handler)
        writer = self._determineWriter(file_handler, file_format)
        if not writer:
            Logger.log("e", "Missing file or mesh writer!")
            return self._onUploadError(T.COULD_NOT_EXPORT)

        stream = io.StringIO() if file_format["mode"] == FileWriter.OutputMode.TextMode else io.BytesIO()
        writer.write(stream, nodes)

        # TODO: Remove extension from the file name, since we are using content types now
        self._sendPrintJob(file_name + "." + file_format["extension"], file_format["mime_type"], stream)

    # TODO: This is yanked right out of ClusterUM3OutputDevice, great candidate for a utility or base class
    def _determineFileFormat(self, file_handler) -> Optional[Dict[str, Union[str, int]]]:
        # Formats supported by this application (file types that we can actually write).
        if file_handler:
            file_formats = file_handler.getSupportedFileTypesWrite()
        else:
            file_formats = CuraApplication.getInstance().getMeshFileHandler().getSupportedFileTypesWrite()

        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        # Create a list from the supported file formats string.
        if not global_stack:
            Logger.log("e", "Missing global stack!")
            return

        machine_file_formats = global_stack.getMetaDataEntry("file_formats").split(";")
        machine_file_formats = [file_type.strip() for file_type in machine_file_formats]
        # Exception for UM3 firmware version >=4.4: UFP is now supported and should be the preferred file format.
        if "application/x-ufp" not in machine_file_formats and Version(self.firmwareVersion) >= Version("4.4"):
            machine_file_formats = ["application/x-ufp"] + machine_file_formats

        # Take the intersection between file_formats and machine_file_formats.
        format_by_mimetype = {f["mime_type"]: f for f in file_formats}

        # Keep them ordered according to the preference in machine_file_formats.
        file_formats = [format_by_mimetype[mimetype] for mimetype in machine_file_formats]

        if len(file_formats) == 0:
            Logger.log("e", "There are no file formats available to write with!")
            raise OutputDeviceError.WriteRequestFailedError(T.WRITE_FAILED)
        return file_formats[0]

    # TODO: This is yanked right out of ClusterUM3OutputDevice, great candidate for a utility or base class
    @staticmethod
    def _determineWriter(file_handler, file_format) -> Optional[FileWriter]:
        # Just take the first file format available.
        if file_handler is not None:
            writer = file_handler.getWriterByMimeType(cast(str, file_format["mime_type"]))
        else:
            writer = CuraApplication.getInstance().getMeshFileHandler().getWriterByMimeType(
                cast(str, file_format["mime_type"])
            )

        if not writer:
            Logger.log("e", "Unexpected error when trying to get the FileWriter")
            return

        return writer

    ##  Get remote printers.
    @pyqtProperty("QVariantList", notify = printersChanged)
    def printers(self):
        return self._printers

    ##  Get remote print jobs.
    @pyqtProperty("QVariantList", notify = printJobsChanged)
    def printJobs(self)-> List[UM3PrintJobOutputModel]:
        return self._print_jobs

    ##  Get remote print jobs that are still in the print queue.
    @pyqtProperty("QVariantList", notify = printJobsChanged)
    def queuedPrintJobs(self) -> List[UM3PrintJobOutputModel]:
        return [print_job for print_job in self._print_jobs
                if print_job.state == "queued" or print_job.state == "error"]

    ##  Get remote print jobs that are assigned to a printer.
    @pyqtProperty("QVariantList", notify = printJobsChanged)
    def activePrintJobs(self) -> List[UM3PrintJobOutputModel]:
        return [print_job for print_job in self._print_jobs if
                print_job.assignedPrinter is not None and print_job.state != "queued"]

    ##  Called when the connection to the cluster changes.
    def connect(self) -> None:
        super().connect()

    ##  Called when the network data should be updated.
    def _update(self) -> None:
        super()._update()
        if self._last_response_time and time() - self._last_response_time < self.CHECK_CLUSTER_INTERVAL:
            return  # avoid calling the cloud too often

        if self._account.isLoggedIn:
            self.setAuthenticationState(AuthState.Authenticated)
            self._api.getClusterStatus(self._device_id, self._onStatusCallFinished)
        else:
            self.setAuthenticationState(AuthState.NotAuthenticated)

    ##  Method called when HTTP request to status endpoint is finished.
    #   Contains both printers and print jobs statuses in a single response.
    def _onStatusCallFinished(self, status: CloudClusterStatus) -> None:
        # Update all data from the cluster.
        self._updatePrinters(status.printers)
        self._updatePrintJobs(status.print_jobs)

    def _updatePrinters(self, printers: List[CloudClusterPrinter]) -> None:
        remote_printers = {p.uuid: p for p in printers}  # type: Dict[str, CloudClusterPrinter]
        current_printers = {p.key: p for p in self._printers}  # type: Dict[str, PrinterOutputModel]

        removed_printer_ids = set(current_printers).difference(remote_printers)
        new_printer_ids = set(remote_printers).difference(current_printers)
        updated_printer_ids = set(current_printers).intersection(remote_printers)

        for printer_guid in removed_printer_ids:
            self._printers.remove(current_printers[printer_guid])

        for printer_guid in new_printer_ids:
            self._addPrinter(remote_printers[printer_guid])

        for printer_guid in updated_printer_ids:
            self._updatePrinter(current_printers[printer_guid], remote_printers[printer_guid])

        self.printersChanged.emit()

    def _addPrinter(self, printer: CloudClusterPrinter) -> None:
        model = PrinterOutputModel(
            PrinterOutputController(self), len(printer.configuration), firmware_version = printer.firmware_version
        )
        self._printers.append(model)
        self._updatePrinter(model, printer)

    def _updatePrinter(self, model: PrinterOutputModel, printer: CloudClusterPrinter) -> None:
        model.updateKey(printer.uuid)
        model.updateName(printer.friendly_name)
        model.updateType(printer.machine_variant)
        model.updateState(printer.status if printer.enabled else "disabled")

        for index in range(0, len(printer.configuration)):
            try:
                extruder = model.extruders[index]
                extruder_data = printer.configuration[index]
            except IndexError:
                break

            extruder.updateHotendID(extruder_data.print_core_id)

            if extruder.activeMaterial is None or extruder.activeMaterial.guid != extruder_data.material.guid:
                material = self._createMaterialOutputModel(extruder_data.material)
                extruder.updateActiveMaterial(material)

    @staticmethod
    def _createMaterialOutputModel(material: CloudClusterPrinterConfigurationMaterial) -> MaterialOutputModel:
        material_manager = CuraApplication.getInstance().getMaterialManager()
        material_group_list = material_manager.getMaterialGroupListByGUID(material.guid) or []

        # Sort the material groups by "is_read_only = True" first, and then the name alphabetically.
        read_only_material_group_list = list(filter(lambda x: x.is_read_only, material_group_list))
        non_read_only_material_group_list = list(filter(lambda x: not x.is_read_only, material_group_list))
        material_group = None
        if read_only_material_group_list:
            read_only_material_group_list = sorted(read_only_material_group_list, key = lambda x: x.name)
            material_group = read_only_material_group_list[0]
        elif non_read_only_material_group_list:
            non_read_only_material_group_list = sorted(non_read_only_material_group_list, key = lambda x: x.name)
            material_group = non_read_only_material_group_list[0]

        if material_group:
            container = material_group.root_material_node.getContainer()
            color = container.getMetaDataEntry("color_code")
            brand = container.getMetaDataEntry("brand")
            material_type = container.getMetaDataEntry("material")
            name = container.getName()
        else:
            Logger.log("w", "Unable to find material with guid {guid}. Using data as provided by cluster"
                       .format(guid = material.guid))
            color = material.color
            brand = material.brand
            material_type = material.material
            name = "Empty" if material.material == "empty" else "Unknown"

        return MaterialOutputModel(guid = material.guid, type = material_type, brand = brand, color = color,
                                   name = name)

    def _updatePrintJobs(self, jobs: List[CloudClusterPrintJob]) -> None:
        remote_jobs = {j.uuid: j for j in jobs}  # type: Dict[str, CloudClusterPrintJob]
        current_jobs = {j.key: j for j in self._print_jobs}  # type: Dict[str, UM3PrintJobOutputModel]

        remote_job_ids = set(remote_jobs)  # type: Set[str]
        current_job_ids = set(current_jobs)  # type: Set[str]

        for removed_job_id in current_job_ids.difference(remote_job_ids):
            self._print_jobs.remove(current_jobs[removed_job_id])

        for new_job_id in remote_job_ids.difference(current_jobs):
            self._addPrintJob(remote_jobs[new_job_id])

        for updated_job_id in current_job_ids.intersection(remote_job_ids):
            self._updateUM3PrintJobOutputModel(current_jobs[updated_job_id], remote_jobs[updated_job_id])

        # We only have to update when jobs are added or removed
        # updated jobs push their changes via their output model
        if remote_job_ids != current_job_ids:
            self.printJobsChanged.emit()

    def _addPrintJob(self, job: CloudClusterPrintJob) -> None:
        try:
            printer = next(p for p in self._printers if job.printer_uuid == p.key)
        except StopIteration:
            return Logger.log("w", "Missing printer %s for job %s in %s", job.printer_uuid, job.uuid,
                              [p.key for p in self._printers])

        model = UM3PrintJobOutputModel(printer.getController(), job.uuid, job.name)
        model.updateAssignedPrinter(printer)
        self._print_jobs.append(model)

    @staticmethod
    def _updateUM3PrintJobOutputModel(model: UM3PrintJobOutputModel, job: CloudClusterPrintJob) -> None:
        model.updateTimeTotal(job.time_total)
        model.updateTimeElapsed(job.time_elapsed)
        model.updateOwner(job.owner)
        model.updateState(job.status)

    def _sendPrintJob(self, file_name: str, content_type: str, stream: Union[io.StringIO, io.BytesIO]) -> None:
        mesh = stream.getvalue()

        request = CloudJobUploadRequest()
        request.job_name = file_name
        request.file_size = len(mesh)
        request.content_type = content_type

        self._api.requestUpload(request, lambda response: self._onPrintJobCreated(mesh, response))

    def _onPrintJobCreated(self, mesh: bytes, job_response: CloudJobResponse) -> None:
        self._api.uploadMesh(job_response, mesh, self._onPrintJobUploaded, self._updateUploadProgress,
                             lambda _: self._onUploadError(T.UPLOAD_ERROR))

    def _onPrintJobUploaded(self, job_id: str) -> None:
        self._api.requestPrint(self._device_id, job_id, self._onUploadSuccess)

    def _updateUploadProgress(self, progress: int):
        if not self._progress_message:
            self._progress_message = Message(
                text = T.SENDING_DATA_TEXT,
                title = T.SENDING_DATA_TITLE,
                progress = -1,
                lifetime = 0,
                dismissable = False,
                use_inactivity_timer = False
            )
        self._progress_message.setProgress(progress)
        self._progress_message.show()

    def _resetUploadProgress(self):
        if self._progress_message:
            self._progress_message.hide()
            self._progress_message = None

    def _onUploadError(self, message: str = None):
        self._resetUploadProgress()
        if message:
            message = Message(
                text = message,
                title = T.ERROR,
                lifetime = 10,
                dismissable = True
            )
            message.show()
        self._sending_job = False  # the upload has finished so we're not sending a job anymore
        self.writeError.emit()

    # Shows a message when the upload has succeeded
    def _onUploadSuccess(self, response: CloudPrintResponse):
        Logger.log("i", "The cluster will be printing this print job with the ID %s", response.cluster_job_id)
        self._resetUploadProgress()
        message = Message(
            text = T.UPLOAD_SUCCESS_TEXT,
            title = T.UPLOAD_SUCCESS_TITLE,
            lifetime = 5,
            dismissable = True,
        )
        message.show()
        self._sending_job = False  # the upload has finished so we're not sending a job anymore
        self.writeFinished.emit()

    ##  TODO: The following methods are required by the monitor page QML, but are not actually available using cloud.
    #   TODO: We fake the methods here to not break the monitor page.

    @pyqtProperty(QObject, notify = printersChanged)
    def activePrinter(self) -> Optional[PrinterOutputModel]:
        if not self._printers:
            return None
        return self._printers[0]

    @pyqtSlot(QObject)
    def setActivePrinter(self, printer: Optional[PrinterOutputModel]) -> None:
        pass

    @pyqtProperty(QUrl, notify = printersChanged)
    def activeCameraUrl(self) -> "QUrl":
        return QUrl()

    @pyqtSlot(QUrl)
    def setActiveCameraUrl(self, camera_url: "QUrl") -> None:
        pass

    @pyqtProperty(bool, notify = printJobsChanged)
    def receivedPrintJobs(self) -> bool:
        return True

    def _onApiError(self, errors: List[CloudErrorObject]) -> None:
        pass  # TODO: Show errors...
